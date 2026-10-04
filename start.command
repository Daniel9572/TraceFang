#!/bin/zsh
set -e

project_root=${0:A:h}
cd "$project_root"
python="$project_root/.venv/bin/python"
if [[ ! -x "$python" ]]; then
  echo "[TraceFang] 项目尚未安装，请先运行 setup.cmd 或执行 uv sync"
  exit 1
fi
exec env PYTHONPATH="$project_root/src" "$python" -m tracefang.service start "$@"
