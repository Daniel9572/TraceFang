#!/bin/zsh
set -e

project_root=${0:A:h}
cd "$project_root"
exec env PYTHONPATH="$project_root/src" "$project_root/.venv/bin/python" -m tracefang.service update "$@"
