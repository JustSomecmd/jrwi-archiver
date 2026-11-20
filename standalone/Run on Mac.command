#!/bin/bash
# Double-click to run the JRWI archiver. The first run sets itself up (takes a minute).
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python isn't installed yet. Get it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close."
  exit 1
fi
if [ ! -x ".venv/bin/python" ]; then
  echo "Setting up for the first time..."
  python3 -m venv .venv || { read -r -p "Could not set up Python. Press Enter to close."; exit 1; }
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet --upgrade -r requirements.txt
.venv/bin/python jrwi_archive.py "$@"
echo
read -r -p "Finished. Press Enter to close."
