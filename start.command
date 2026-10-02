#!/bin/bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$project_dir"
if [ ! -x .venv/bin/python ]; then
  chosen_python=""
  for python_candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$python_candidate" >/dev/null 2>&1 && "$python_candidate" -c 'import sys; raise SystemExit(sys.version_info < (3,11))' 2>/dev/null; then
      chosen_python="$python_candidate"
      break
    fi
  done
  if [ -z "$chosen_python" ]; then
    echo '需要 Python 3.11 或更新版本。安装后重新运行此文件。'
    exit 1
  fi
  "$chosen_python" -m venv .venv
fi
if ! .venv/bin/python -c 'import streamlit, httpx, dotenv' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.lock.txt
fi
exec .venv/bin/python -m streamlit run app.py --server.address 127.0.0.1 --server.port "${NOVEL_PORT:-8501}" --browser.gatherUsageStats false
