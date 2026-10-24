#!/bin/sh
# Repeatable Python development setup. Run from any directory.
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
python3 -m venv .venv
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$repo_dir/.venv/pip-cache}"
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m pip check
.venv/bin/python -m tidalbind examples/performance.tidal --validate
.venv/bin/python -m tidalbind examples/performance.tidal --dry-run --replay examples/replay.jsonl --missing-values defer
.venv/bin/python -m pytest -q
