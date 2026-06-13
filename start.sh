#!/bin/zsh
cd "$(dirname "$0")"
source .venv/bin/activate
exec python3 server.py
