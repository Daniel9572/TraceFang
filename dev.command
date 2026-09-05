#!/bin/zsh
set -e

project_root=${0:A:h}
cd "$project_root"
exec python3 scripts/run-local.py --dev
