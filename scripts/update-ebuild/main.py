#!/usr/bin/env python3

import re
import subprocess
import urllib.request
import json
import sys
from pathlib import Path
from shutil import copyfile

# === CONFIG ===
OVERLAY = Path("/var/db/repos/localrepo")  # adjust if needed


def get_latest_tag(repo: str) -> str | None:
    """Fetch latest tag from GitHub API. Returns None if no tags exist."""
    url = f"https://api.github.com/repos/{repo}/tags"
    with urllib.request.urlopen(url) as r:
        tags = json.load(r)
    if not tags:
        return None
    return tags[0]["name"].lstrip("v")  # strip leading v if present


def get_existing_versions(pkgdir: Path, pn: str) -> list[str]:
    """Return sorted list of existing ebuild versions."""
    versions = []
    for f in pkgdir.glob("*.ebuild"):
        m = re.match(rf"{pn}-(.+)\.ebuild", f.name)
        if m:
            versions.append(m.group(1))
    return sorted(versions)


def extract_repo_from_ebuild(ebuild_path):
    with open(ebuild_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Match EGIT_REPO_URI and capture only the GitHub part
    m = re.search(r'EGIT_REPO_URI="https://github.com/([^"]+)"', content)
    if not m:
        raise ValueError("Could not find EGIT_REPO_URI in ebuild")

    repo = m.group(1).strip()
    repo = repo.rstrip(".git")
    return repo


def bump_src_uri(text: str, repo: str, oldver: str, newver: str) -> str:
    """Replace the version in SRC_URI tarball with new version."""
    pattern = re.compile(rf"https://github.com/{repo}/archive/refs/tags/([^ ]+)")
    return pattern.sub(
        f"https://github.com/{repo}/archive/refs/tags/{newver}.tar.gz", text
    ).replace(f"{oldver}.tar.gz", f"{newver}.tar.gz")


def write_new_ebuild(
    pkgdir: Path, pn: str, base_ebuild: Path, oldver: str, newver: str, repo: str
):
    """Copy base ebuild, update SRC_URI, regen manifest."""
    newfile = pkgdir / f"{pn}-{newver}.ebuild"
    if newfile.exists():
        print(f"Ebuild {newfile} already exists")
        return
    text = base_ebuild.read_text()
    if oldver != "9999" and newver != "9999":
        text = bump_src_uri(text, repo, oldver, newver)
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
    latest_tag = get_latest_tag(repo)

    if latest_tag:
        print(f"Latest upstream tag: {latest_tag}")
        if latest_tag in versions:
            print("Already up to date.")
            return
        write_new_ebuild(pkgdir, pn, base_ebuild, basever, latest_tag, repo)
    else:
        print("No tags found upstream, falling back to 9999 live ebuild.")
        if "9999" in versions:
            print("9999 ebuild already exists.")
            return
        write_new_ebuild(pkgdir, pn, base_ebuild, basever, "9999", repo)


if __name__ == "__main__":
    main()
