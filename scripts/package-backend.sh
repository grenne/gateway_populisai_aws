#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
rm -rf package function.zip _venv_pack
mkdir package
python3 -m venv _venv_pack
# shellcheck disable=SC1091
source _venv_pack/bin/activate
pip install -r requirements.txt -t package/
deactivate
rm -rf _venv_pack
cp handler.py package/
(cd package && zip -r ../function.zip .)
echo "Gerado: $ROOT/backend/function.zip"
