#!/usr/bin/env bash
# Print the test files the integration lane runs, one per line, sorted: tests/test_real_databases.py and every
# tests/*_integration.py. A new integration test needs no edit to test-integration.sh: name the file *_integration.py.
#
# Files that carry the integration marker but are not discovered here belong to another lane, and are named in
# tests/test_integration_discovery.py with the reason. That test fails if a marked file is in no lane.
#
# usage: integration-files.sh [tests-dir]   (default: tests next to this script's parent)
set -euo pipefail

tests_dir="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/tests}"
base="$(basename "${tests_dir}")"

{
    if [[ -f "${tests_dir}/test_real_databases.py" ]]; then
        echo "${base}/test_real_databases.py"
    fi
    for file in "${tests_dir}"/*_integration.py; do
        if [[ -e "${file}" ]]; then
            echo "${base}/$(basename "${file}")"
        fi
    done
} | LC_ALL=C sort -u
