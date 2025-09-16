#!/usr/bin/env python3
import re
import subprocess
import urllib.request
import json
import sys
from pathlib import Path

# === CONFIG ===
OVERLAY = Path("/var/db/repos/localrepo")  # adjust if needed


# === HELPERS ===
def normalize_tag_to_pv(tag: str) -> str:
    """Convert GitHub tag into a valid Gentoo PV string (no commit hash)."""
    tag = tag.lstrip("v")
    # Normalize "Release" markers
    tag = re.sub(r"[._-]Release[._-]", "_", tag)
    # Keep dots for version numbers, replace other separators with underscores
    tag = re.sub(r"[^0-9a-zA-Z.]+", "_", tag)
    # Collapse multiple underscores
    tag = re.sub(r"__+", "_", tag)
    # Trim trailing underscores or dots
    tag = tag.strip("._")
    # Drop trailing commit hashes like `_8a87a79b`
    tag = re.sub(r"_([0-9a-f]{6,})$", "", tag)

    return tag


def get_latest_release_tag(repo: str) -> str | None:
    """Fetch tag name of latest release. Returns None if no releases exist."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        with urllib.request.urlopen(url) as r:
            release = json.load(r)
        tag = release.get("tag_name")
        if tag:
            return tag
    except Exception:
        return None
    return None


def get_latest_tag(repo: str) -> str | None:
    """Fetch most recent tag (fallback if no releases)."""
    url = f"https://api.github.com/repos/{repo}/tags"
    with urllib.request.urlopen(url) as r:
        tags = json.load(r)
    if not tags:
        return None
    return tags[0]["name"]


def get_existing_versions(pkgdir: Path, pn: str) -> list[str]:
    """Return sorted list of existing ebuild versions."""
    versions = []
    for f in pkgdir.glob("*.ebuild"):
        m = re.match(rf"{pn}-(.+)\.ebuild", f.name)
        if m:
            versions.append(m.group(1))
    return sorted(versions)


def extract_repo_from_ebuild(ebuild_path: Path) -> str:
    """Extract GitHub org/repo from EGIT_REPO_URI."""
    with open(ebuild_path, "r", encoding="utf-8") as f:
        content = f.read()

    m = re.search(r'EGIT_REPO_URI="https://github.com/([^"]+)"', content)
    if not m:
        raise ValueError("Could not find EGIT_REPO_URI in ebuild")

    repo = m.group(1).strip()
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo


def bump_src_uri(text: str, repo: str, oldver: str, newtag: str) -> str:
    """Replace the version in SRC_URI tarball with new upstream tag."""
    pattern = re.compile(rf"https://github.com/{repo}/archive/refs/tags/([^ ]+)")
    text = pattern.sub(
        f"https://github.com/{repo}/archive/refs/tags/{newtag}.tar.gz", text
    )
    text = text.replace(f"{oldver}.tar.gz", f"{newtag}.tar.gz")
    return text


def set_keywords(text: str, newver: str) -> str:
    """Set ~amd64 for all versions except 9999."""
    if newver != "9999":
        if re.search(r"^KEYWORDS=", text, re.MULTILINE):
            text = re.sub(
                r"^KEYWORDS=.*", 'KEYWORDS="~amd64"', text, flags=re.MULTILINE
            )
        else:
            text = re.sub(
                r"^(EAPI=.*\n)", r'\1KEYWORDS="~amd64"\n', text, flags=re.MULTILINE
            )
    return text


def write_new_ebuild(
    pkgdir: Path,
    pn: str,
    base_ebuild: Path,
    oldver: str,
    newpv: str,
    newtag: str,
    repo: str,
):
    """Copy base ebuild, update SRC_URI, set KEYWORDS, regenerate manifest."""
    newfile = pkgdir / f"{pn}-{newpv}.ebuild"
    if newfile.exists():
        print(f"Ebuild {newfile} already exists")
        return
    text = base_ebuild.read_text()
    if oldver != "9999" and newpv != "9999":
        text = bump_src_uri(text, repo, oldver, newtag)
    text = set_keywords(text, newpv)
    newfile.write_text(text)
    print(f"Created {newfile}")
    subprocess.run(["ebuild", str(newfile), "manifest"], check=True)


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <category/package>")
        sys.exit(1)

    catpkg = sys.argv[1]
    category, pn = catpkg.split("/")
    pkgdir = OVERLAY / category / pn

    if not pkgdir.exists():
        print(f"Package directory {pkgdir} not found in overlay")
        sys.exit(1)

    versions = get_existing_versions(pkgdir, pn)
    if not versions:
        print(f"No ebuilds found in {pkgdir}")
        sys.exit(1)

    basever = versions[-1]
    base_ebuild = pkgdir / f"{pn}-{basever}.ebuild"
    print(f"Using {base_ebuild} as template")

    repo = extract_repo_from_ebuild(base_ebuild)
    if not repo:
        print("Could not detect GitHub repo from ebuild")
        sys.exit(1)

    print(f"Detected repo: {repo}")

    latest_tag = get_latest_release_tag(repo)
    if latest_tag:
        print(f"Latest upstream release tag: {latest_tag}")
    else:
        latest_tag = get_latest_tag(repo)
        if latest_tag:
            print(f"Latest upstream tag (no releases found): {latest_tag}")

    if latest_tag:
        newpv = normalize_tag_to_pv(latest_tag)
        print(f"Normalized Gentoo PV: {newpv}")
        if newpv in versions:
            print("Already up to date.")
            return
        write_new_ebuild(pkgdir, pn, base_ebuild, basever, newpv, latest_tag, repo)
    else:
        print("No tags or releases found upstream, falling back to 9999 live ebuild.")
        if "9999" in versions:
            print("9999 ebuild already exists.")
            return
        write_new_ebuild(pkgdir, pn, base_ebuild, basever, "9999", "9999", repo)


if __name__ == "__main__":
    main()
