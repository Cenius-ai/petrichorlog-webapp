#!/usr/bin/env bash
# PetrichorLog setup: installs the pinned test runner, installs the CLI as a
# console script, and proves the module imports. It installs nothing at the
# system level, runs no services, and exits — it never starts anything long
# running.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

printf 'Upgrading packaging tooling...\n'
"$PY" -m pip install --upgrade pip setuptools wheel

printf 'Installing pinned dependencies...\n'
"$PY" -m pip install -r requirements.txt

printf 'Installing the petrichorlog console script (editable)...\n'
"$PY" -m pip install -e .

printf 'Checking the CLI imports cleanly...\n'
"$PY" -c "import petrichorlog; print('petrichorlog', petrichorlog.__version__, 'ready')"

cat <<'EOF'

Setup complete.
  run the CLI      : python3 petrichorlog.py --help    (or: petrichorlog --help)
  seed + demo data : bash demo.sh
  run the tests    : python3 -m pytest
EOF
