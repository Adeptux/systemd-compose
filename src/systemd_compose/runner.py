from __future__ import annotations

import subprocess


def run_command(command: list[str], *, check: bool = True) -> int:
    return subprocess.run(command, check=check).returncode


def run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)
