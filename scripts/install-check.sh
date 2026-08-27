#!/usr/bin/env bash
set -euo pipefail

bash scripts/prepare-runtime-wheel.sh
api_tmp="$(mktemp -d)"
trap 'rm -rf "${api_tmp}"' EXIT

uv venv "${api_tmp}/venv"
uv pip install --python "${api_tmp}/venv/bin/python" --require-hashes --requirements .build/requirements.txt
uv pip install --python "${api_tmp}/venv/bin/python" --no-deps .build/private/*.whl dist/*.whl
"${api_tmp}/venv/bin/python" -c 'import api.api; import api.config; import api.nlq.tools'
