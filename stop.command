#!/bin/zsh
set -e

project_root=${0:A:h}
cd "$project_root"
python="$project_root/.venv/bin/python"
exec env PYTHONPATH="$project_root/src" "$python" -m tracefang.service stop
