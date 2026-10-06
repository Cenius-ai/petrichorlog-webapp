#!/usr/bin/env bash
# PetrichorLog end-to-end demo.
#
# Seeds a scratch store through the real CLI (no fixtures, no network) and walks
# every command, including the failure paths and the local passphrase lock.
# Nothing here needs a terminal or any input; it always exits 0.
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
CLI="petrichorlog.py"
DEMO_DIR="${PETRICHORLOG_DEMO_DIR:-.petrichorlog-demo}"
YEAR="$(date +%Y)"

rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR"

step() { printf '\n$ petrichorlog %s\n' "$*"; }

# run: a happy path, stdin closed so the interactive wizard can never block.
run() {
  step "$@"
  "$PY" "$CLI" "$@" < /dev/null
}

# show_failure: expects a non-zero exit and prints the code.
show_failure() {
  step "$@"
  "$PY" "$CLI" "$@" < /dev/null
  printf '[exit %d]\n' "$?"
}

printf 'PetrichorLog demo — %s\n' "$(date '+%Y-%m-%d')"
printf 'scratch store: %s\n' "$DEMO_DIR/petrichorlog.json"

export PETRICHORLOG_STORE="$DEMO_DIR/petrichorlog.json"

run --version
run log list

# ---------------------------------------------------------------- seed events
printf '\n# Seed a season of rain events through the CLI\n'
run log add --start "$YEAR-03-04T06:20" --rain 0.4 --minutes 8 \
  --notes "first hint of damp dust on the path"
run log add --start "$YEAR-03-19T17:45" --rain 6.2 --minutes 35 \
  --notes "warm ground, smell arrived late"
run log add --start "$YEAR-04-12T05:10" --rain 21.5 --minutes 95 \
  --notes "slow steady front, garden soaked"
run log add --start "$YEAR-05-28T20:05" --rain 3.1 --minutes 22 \
  --notes "passing shower, pavement barely wet"
run log add --start "$YEAR-06-09T14:00" --rain 34.0 --minutes 150 \
  --notes "storm cell overhead, gutters overflowing"
run log add --start "$YEAR-06-21T07:30" --rain 12.0 --minutes 45 \
  --notes "drizzle on warm soil, geosmin all morning"
run log add --start "$YEAR-07-02T22:15" --rain 0.8 --minutes 12 \
  --notes "spits of rain, dry air mostly"
run log add --start "$YEAR-08-18T09:40" --rain 47.6 --minutes 210 \
  --notes "overnight front stalled over the valley"

printf '\n# Soil moisture readings, appended to the seeded events\n'
run log note 2 --pre 28 --depth 10 --obs "dry crust, cracked at the edges"
run log note 2 --post 44 --depth 10 --obs "soaked through to the trowel"
run log note 3 --pre 31 --depth 20 --obs "crumbly, pale"
run log note 3 --post 58 --depth 20 --obs "dark and heavy, worms at the top"
run log note 4 --pre 22 --depth 10 --obs "dusty, repellent"
run log note 5 --post 51 --depth 10 --obs "runoff pooling at the bed edge"
run log note 5 --relative 1.32 --depth 30 --obs "relative probe at 30 cm"
run log note 6 --pre 26 --depth 15 --obs "leaf litter damp but soft underneath"

printf '\n# Reading the log back\n'
run log list
run log show 1
run log show 5
run log season --year "$YEAR"

printf '\n# Machine-readable output (for scripts)\n'
step log list --json
"$PY" "$CLI" log list --json < /dev/null | head -20

# ------------------------------------------------------------- failure paths
printf '\n# Flag-mode failures: exactly one line, non-zero exit, nothing saved\n'
show_failure log add --minutes 30 --end 14:00 --rain 1
show_failure log add --start 09:00 --end 08:30 --rain 1
show_failure log add --rain -1 --minutes 10
show_failure log add --intensity 6 --rain 1 --minutes 10
show_failure log note 1 --pre 101
show_failure log note 1 --relative -1
show_failure log show 999

printf '\n# A corrupt store is moved aside, never overwritten\n'
printf '{ broken json' > "$DEMO_DIR/broken.json"
show_failure --store "$DEMO_DIR/broken.json" log list
printf 'backups kept: %s\n' "$(ls "$DEMO_DIR" | grep '^broken.json.bak-' | tr '\n' ' ')"

# -------------------------------------------------------------- passphrase
printf '\n# Local passphrase lock (PBKDF2-HMAC-SHA256, stored in the file)\n'
PASSFILE="$DEMO_DIR/passphrase.txt"
printf 'cenius\n' > "$PASSFILE"
printf 'wrong-passphrase\n' > "$DEMO_DIR/wrong.txt"
export PETRICHORLOG_STORE="$DEMO_DIR/private.json"

printf '$ cat passphrase.txt | petrichorlog --passphrase-stdin log add ...\n'
"$PY" "$CLI" --passphrase-stdin log add --rain 5 --minutes 20 \
  --start "$YEAR-09-01T07:00" --notes "kept behind the local passphrase" < "$PASSFILE"

printf '\n$ cat wrong-passphrase.txt | petrichorlog --passphrase-stdin log list\n'
"$PY" "$CLI" --passphrase-stdin log list < "$DEMO_DIR/wrong.txt"
printf '[exit %d]\n' "$?"

printf '\n$ cat passphrase.txt | petrichorlog --passphrase-stdin log list\n'
"$PY" "$CLI" --passphrase-stdin log list < "$PASSFILE"
printf '[exit %d]\n' "$?"

printf '\nDemo complete. Scratch stores live in %s — delete that directory to reset.\n' "$DEMO_DIR"
