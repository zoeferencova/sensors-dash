#!/usr/bin/env python3
"""Bootstrap and launch the Botic flood monitoring dashboard.

    python run.py     (Windows)
    python3 run.py    (macOS / Linux)

Creates .venv on first run, installs requirements.txt into it, picks a free
port and opens the dashboard in a browser. Safe to re-run: an existing
environment with up-to-date dependencies is reused, so later launches start
immediately.

Deliberately depends on nothing but the standard library, so the only thing
a new machine needs is Python itself.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import threading
import venv
import webbrowser
from pathlib import Path

MIN_PYTHON = (3, 10)  # app code uses `X | None` type syntax
DEFAULT_PORT = 8060

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
APP = ROOT / "app" / "main.py"
# Which requirements.txt this environment was last installed from, so pip is
# re-run when (and only when) that file changes.
STAMP = VENV_DIR / ".requirements-hash"


def fail(message: str) -> "None":
    print(f"\nERROR: {message}\n", file=sys.stderr)
    raise SystemExit(1)


def venv_python() -> Path:
    """The interpreter inside .venv.

    This is the one platform difference that actually matters here: Windows
    virtual environments put the interpreter in Scripts/, every other
    platform in bin/. Resolving it at runtime is what lets one checkout work
    on both without a per-machine config file.
    """
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv() -> Path:
    python = venv_python()
    if python.exists():
        return python
    print(f"Creating virtual environment in {VENV_DIR.name}/ ...")
    venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    python = venv_python()
    if not python.exists():
        fail(f"virtual environment was created but has no interpreter at {python}")
    return python


def ensure_dependencies(python: Path) -> None:
    if not REQUIREMENTS.exists():
        fail(f"{REQUIREMENTS.name} not found next to run.py")
    wanted = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if STAMP.exists() and STAMP.read_text(encoding="utf-8").strip() == wanted:
        return
    print("Installing dependencies (the first run can take a minute) ...")
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip", "-q"], check=False)
    if subprocess.run([str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS)]).returncode != 0:
        fail("pip could not install the dependencies - see the output above")
    STAMP.write_text(wanted, encoding="utf-8")


def free_port(preferred: int) -> int:
    """`preferred` if it is available, otherwise any free port.

    Falling back matters because a dashboard left running in another window
    is the normal way this fails, and 'port already in use' is an unhelpful
    thing to hand someone who just wants to look at the project.
    """
    for candidate in (preferred, 0):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return probe.getsockname()[1]
    fail("could not find a free port")
    return preferred  # unreachable; keeps type checkers happy


def main() -> None:
    if sys.version_info < MIN_PYTHON:
        fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required, but this is "
            f"{sys.version.split()[0]} ({sys.executable}). Install a newer Python and re-run."
        )
    if not APP.exists():
        fail(f"could not find {APP.relative_to(ROOT)} - run this script from the project root")

    python = ensure_venv()
    ensure_dependencies(python)

    port = free_port(DEFAULT_PORT)
    if port != DEFAULT_PORT:
        print(f"Port {DEFAULT_PORT} is in use - starting on {port} instead.")
    url = f"http://127.0.0.1:{port}"

    # DASH_DEBUG=0 turns off the reloader and the dev-tools overlay, so the
    # dashboard opens clean. Running `python app/main.py` directly still gets
    # debug mode, which is what you want while developing.
    env = dict(os.environ, PORT=str(port), DASH_DEBUG="0")
    threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    print(f"\nBotic flood monitoring dashboard -> {url}    (Ctrl+C to stop)\n")
    try:
        raise SystemExit(subprocess.run([str(python), str(APP)], env=env).returncode)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
