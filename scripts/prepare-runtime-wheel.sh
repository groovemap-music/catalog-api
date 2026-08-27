#!/usr/bin/env bash
set -euo pipefail

runtime_repo="${GROOVEMAP_RUNTIME_REPO:-../python-libraries}"
expected="28fa329702bc76896cc54ab8d05ec5b1bd3d929e"

test -d "${runtime_repo}/.git"
actual="$(git -C "${runtime_repo}" rev-parse HEAD)"
test "${actual}" = "${expected}"
test -z "$(git -C "${runtime_repo}" status --short)"

mkdir -p .build/private
find .build/private -type f -name '*.whl' -delete
uv build --wheel --out-dir .build/private "${runtime_repo}"
uv build --wheel --out-dir .build/private "${runtime_repo}/agent-tools"
uv export \
  --frozen \
  --no-dev \
  --no-emit-project \
  --no-emit-package groovemap-runtime \
  --no-emit-package groovemap-agent-tools \
  --output-file .build/requirements.txt \
  >/dev/null
