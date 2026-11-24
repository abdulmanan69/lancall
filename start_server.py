#!/usr/bin/env python3
"""Start LAN Call.

Creates a private Python environment in .venv the first time (and whenever
requirements.txt changes), then runs the server. Any arguments are passed on:

    python start_server.py --port 443 --pin 2468

Run ``python start_server.py --help`` for all options.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV / ".requirements-hash"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_environment() -> Path:
    if sys.version_info < (3, 11):
        sys.exit("LAN Call needs Python 3.11 or newer. You have " + sys.version.split()[0])
    python = venv_python()
    if not python.exists():
        print("First run: creating the Python environment (.venv)...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    wanted = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if not STAMP.exists() or STAMP.read_text().strip() != wanted:
        print("Installing dependencies...")
        try:
            subprocess.check_call(
                [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(REQUIREMENTS)]
            )
        except subprocess.CalledProcessError:
            sys.exit(
                "Could not install dependencies. This step needs internet access once.\n"
                "On an offline machine, copy the .venv folder from a machine with the same\n"
                "operating system and Python version."
            )
        STAMP.write_text(wanted)
    return python


def main() -> int:
    python = ensure_environment()
    os.chdir(ROOT)
    try:
        return subprocess.call([str(python), "-m", "lancall", *sys.argv[1:]])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
