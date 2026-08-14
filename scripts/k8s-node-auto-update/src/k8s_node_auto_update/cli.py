"""Install available APT upgrades and notify the node operator."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_NTFY_URL = "https://ntfy.nesbitt.rocks/servers"
DEFAULT_REBOOT_MARKER = Path("/var/run/reboot-required")
APT_UPGRADE_COMMAND = (
    "apt-get",
    "--assume-yes",
    "-o",
    "Dpkg::Options::=--force-confdef",
    "-o",
    "Dpkg::Options::=--force-confold",
    "upgrade",
)


def run_command(command: Sequence[str]) -> str:
    """Run an APT command and return standard output on success."""
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DEBIAN_FRONTEND": "noninteractive",
            "LC_ALL": "C",
        },
    )
    return completed.stdout


def extract_upgradable_packages(simulation_output: str) -> list[str]:
    """Return unique package names emitted by ``apt-get --simulate``."""
    packages: list[str] = []
    seen: set[str] = set()
    for line in simulation_output.splitlines():
        match = re.match(r"^Inst\s+(\S+)", line)
        if match and match.group(1) not in seen:
            package = match.group(1)
            seen.add(package)
            packages.append(package)
    return packages


def notify(message: str, *, environment: Mapping[str, str] | None = None) -> bool:
    """Send an ntfy notification without exposing the authentication token."""
    configured_environment = os.environ if environment is None else environment
    token = configured_environment.get("NTFY_AUTH_TOKEN")
    if not token:
        raise ValueError("NTFY_AUTH_TOKEN must be configured")

    request = Request(
        configured_environment.get("NTFY_URL", DEFAULT_NTFY_URL),
        data=json.dumps({"message": message}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (HTTPError, URLError, OSError) as error:
        print(f"Unable to send ntfy notification: {error}", file=sys.stderr)
        return False


def main(
    *,
    reboot_marker: Path = DEFAULT_REBOOT_MARKER,
    hostname: str | None = None,
    environment: Mapping[str, str] | None = None,
    user_id: int | None = None,
) -> int:
    """Apply package updates, then mark the host as requiring a reboot."""
    configured_environment = os.environ if environment is None else environment
    effective_user_id = os.geteuid() if user_id is None else user_id
    node_name = socket.gethostname() if hostname is None else hostname

    if effective_user_id != 0:
        print("Root privileges are required. Run this program with sudo.", file=sys.stderr)
        return 1

    try:
        if not configured_environment.get("NTFY_AUTH_TOKEN"):
            raise ValueError("NTFY_AUTH_TOKEN must be configured")

        if reboot_marker.exists():
            notify(
                f"{node_name}: Node already scheduled for reboot. Skipping updates.",
                environment=configured_environment,
            )
            return 0

        print("Updating package cache...")
        run_command(["apt-get", "update"])
        planned_upgrades = extract_upgradable_packages(
            run_command(["apt-get", "--simulate", "--quiet=2", "upgrade"])
        )
        if not planned_upgrades:
            notify(
                f"{node_name}: No updates available.",
                environment=configured_environment,
            )
            return 0

        notify(
            f"{node_name}: Updates will be applied: {', '.join(planned_upgrades)}",
            environment=configured_environment,
        )
        print("Performing package upgrade...")
        run_command(APT_UPGRADE_COMMAND)
        reboot_marker.touch(exist_ok=True)
        notify(
            f"{node_name}: Updates applied; node is marked for reboot.",
            environment=configured_environment,
        )
        return 0
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"Package update failed with exit code {error.returncode}.", file=sys.stderr)
        return error.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
