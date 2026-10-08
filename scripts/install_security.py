#!/usr/bin/env python3
"""Check native Linux security requirements; optionally install one AppArmor profile."""

import argparse
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_NAME = "babata-native"
PROFILE_SOURCE = ROOT / "deploy/security" / PROFILE_NAME
PROFILE_TARGET = Path("/etc/apparmor.d") / PROFILE_NAME


class CheckError(Exception):
    pass


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise CheckError(f"Required check failed: {command[0]}")
    return result.stdout.strip()


def native_security_check(install: bool) -> None:
    if platform.system() != "Linux":
        raise CheckError(
            "Run on the target Linux host; AppArmor is required for native command execution"
        )
    if not shutil.which("docker") or not shutil.which("apparmor_parser"):
        raise CheckError("Docker Compose and the target host's AppArmor parser are required")
    options = json.loads(run(["docker", "info", "--format", "{{json .SecurityOptions}}"]))
    if not any("apparmor" in item for item in options) or not any(
        "seccomp" in item for item in options
    ):
        raise CheckError("Docker must enforce AppArmor and seccomp; do not disable either")
    run(["docker", "compose", "version"])
    run(["apparmor_parser", "-Q", "-T", "-K", str(PROFILE_SOURCE)])
    seccomp = json.loads((ROOT / "deploy/security/seccomp-native.json").read_text())
    if seccomp.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise CheckError("The native seccomp policy must have its restrictive default")
    if PROFILE_TARGET.is_symlink():
        raise CheckError("Existing AppArmor destination is a symlink; inspect it manually")
    if PROFILE_TARGET.exists() and PROFILE_TARGET.read_bytes() != PROFILE_SOURCE.read_bytes():
        raise CheckError("A different babata-native profile exists; refusing to overwrite it")
    try:
        loaded = Path("/sys/kernel/security/apparmor/profiles").read_text()
    except OSError:
        raise CheckError(
            "Cannot inspect loaded AppArmor profiles; use sudo for this check"
        ) from None
    existing_modes = [line for line in loaded.splitlines() if line.startswith(PROFILE_NAME + " (")]
    if existing_modes and not PROFILE_TARGET.exists():
        raise CheckError(
            "A loaded profile has no matching policy file; inspect it before installing"
        )
    if existing_modes and PROFILE_NAME + " (enforce)" not in existing_modes:
        raise CheckError("An existing babata-native policy is not enforcing; inspect it manually")
    if not install and not PROFILE_TARGET.exists():
        raise CheckError("babata-native is absent; review then run with sudo and --install")
    if install:
        if os.geteuid() != 0:
            raise CheckError("The --install operation requires sudo on the target host")
        if not PROFILE_TARGET.exists():
            descriptor = os.open(PROFILE_TARGET, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(PROFILE_SOURCE.read_bytes())
            os.chown(PROFILE_TARGET, 0, 0)
            os.chmod(PROFILE_TARGET, 0o644)
    if PROFILE_NAME + " (enforce)" not in loaded.splitlines():
        if not install:
            raise CheckError(
                "babata-native is not enforcing; review then run with sudo and --install"
            )
        run(["apparmor_parser", "-a", "-T", "-K", str(PROFILE_TARGET)])
        loaded = Path("/sys/kernel/security/apparmor/profiles").read_text()
        if PROFILE_NAME + " (enforce)" not in loaded.splitlines():
            raise CheckError("The installed AppArmor profile is not enforcing")
    print("AppArmor profile syntax, enforcing mode, Docker security and seccomp structure checked.")
    print(
        "This does not prove native sandbox execution or host boot persistence; "
        "run deployment acceptance."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Read-only preflight; default")
    modes.add_argument(
        "--install", action="store_true", help="Exclusively install the reviewed profile if absent"
    )
    args = parser.parse_args()
    try:
        native_security_check(args.install)
    except (CheckError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        # Docker output may contain environment information; never relay it.
        message = (
            str(error) if isinstance(error, CheckError) else "Security preflight could not complete"
        )
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
