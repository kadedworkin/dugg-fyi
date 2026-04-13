#!/bin/bash
# Thin wrapper to call Dugg operations from outside the venv
cd ~/dugg-fyi
exec uv run python3 -m dugg.cli "$@"
