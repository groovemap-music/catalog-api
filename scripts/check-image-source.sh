#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! git -C "${repo_root}" rev-parse --verify 'HEAD^{commit}' >/dev/null 2>&1; then
  echo "Refusing to label an image without a verifiable source revision." >&2
  exit 2
fi

# Provenance identifies the committed first-party tree. Hosted CI adds an
# untracked dependency checkout, and image preparation creates ignored build
# artifacts; neither may affect the revision label. Any tracked-file change does.
if ! git -C "${repo_root}" diff --quiet HEAD --; then
  echo "Refusing to label an image from modified tracked source." >&2
  exit 2
fi
