#!/usr/bin/env python3
"""Install the paper extractor into this project's .venv on another computer."""
from pathlib import Path
import subprocess
import sys
import venv

root = Path(__file__).resolve().parents[1]
environment = root / ".venv"
if not environment.exists():
    venv.EnvBuilder(with_pip=True).create(environment)
python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
subprocess.run([str(python), "-m", "pip", "install", "-r", str(root / "requirements.txt")], check=True)
print("Ready. Run: python3 scripts/papers.py index")
