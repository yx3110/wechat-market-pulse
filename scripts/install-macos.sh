#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
python_command=''
for candidate in python3.13 python3.12 python3.11 python3.14 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,15)' 2>/dev/null; then
    python_command="$candidate"
    break
  fi
done
if [[ -z "$python_command" ]]; then
  echo '需要 64 位 Python 3.11–3.14。安装后重新运行本脚本。' >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then "$python_command" -m venv .venv; fi
.venv/bin/python -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,15), "现有 .venv 版本不受支持，请保留数据后重建此虚拟环境"'
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[media]'
.venv/bin/wechat-pulse --json demo --output ./demo-output
.venv/bin/wechat-pulse --json doctor
echo '安装完成。示例：demo-output/briefing.html；下一步见 docs/INSTALL.md。'
