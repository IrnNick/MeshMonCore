#!/bin/bash
# Dev runner: creates .venv, installs dependencies, launches MeshMonCore.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3.12 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python -m meshmoncore "$@"
