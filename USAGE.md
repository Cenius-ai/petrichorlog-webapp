# Using PetrichorLog

Every command is run as `python3 petrichorlog.py …` (or `petrichorlog …` after
`bash install.sh`). `--store PATH` and `--passphrase-stdin` may be written before
or after the subcommand.

```
petrichorlog [--version] [--store PATH] [--passphrase-stdin] COMMAND …
    log add     log a rain event (wizard, or flags)
    log list    list events, newest first
    log show    full detail for one event
    log note    append a soil-moisture reading
    log season  monthly seasonal pattern for a year
```

## Log a rain event

Flag mode never prompts and never blocks:

```bash
python3 petrichorlog.py log add --rain 4 --minutes 30 --intensity 3
python3 petrichorlog.py log add --start 09:00 --minutes 30 --rain 8.5 \
    --notes "smell arrived ten minutes after the first drops"
python3 petrichorlog.py log add --start 09:00 --end 10:30 --rain 2.5
```

* `--start` takes `HH:MM` (today) or ISO-8601 (`2025-06-14T09:00`); blank = now.
* `--minutes` **or** `--end` — never both. `--end` `HH:MM` lands on the same local
  day as the start and must be later; use ISO-8601 for a window that crosses
  midnight. Duration is the ceiling of `(end - start) / 60` minutes.
* `--rain` is millimetres (default 0), `--intensity` is 0-5 (default: suggested).

Interactive mode — run it with no flags — walks the same fields with prompts,
prints the whole intensity scale, and re-asks on a bad value instead of exiting:

```bash
python3 petrichorlog.py log add
# Start (HH:MM or ISO-8601, blank for now):
# Duration in minutes (blank to give an end time instead) [30]:
# Rain in mm [0]:
# ... Petrichor intensity scale ...
# Suggested intensity: 3 — Earthy: clear petrichor, wet soil and geosmin aroma
# Intensity 0-5 [3]:
```

### The petrichor scale

| Level | Name | Sensory descriptor |
| --- | --- | --- |
| 0 | None | no petrichor, dry air, no earthy smell |
| 1 | Faint | barely detectable hint of damp dust if you inhale deeply |
| 2 | Mild | light earthy scent noticeable when close to soil |
| 3 | Earthy | clear petrichor, wet soil and geosmin aroma |
| 4 | Strong | prominent petrichor with ozone and fresh rain on warm ground |
| 5 | Intense | overpowering geosmin, almost metallic |

The suggested level is deterministic:
`score = (rain_mm / 10) + (duration_min / 60)`, then
`< 0.2 → 0`, `< 0.5 → 1`, `< 1.0 → 2`, `< 2.0 → 3`, `< 3.0 → 4`, else `5`.
`log show` prints the chosen level next to the suggestion and says whether it was
overridden.

## Soil moisture readings

Readings are appended to an event, before or after the rain:

```bash
python3 petrichorlog.py log note 1 --pre 31 --depth 10 --obs "dry crust, cracked"
python3 petrichorlog.py log note 1 --post 45 --depth 10 --obs "soaked through"
python3 petrichorlog.py log note 1 --post 45 --relative 1.45 --depth 30 --obs "probe"
```

* `--pre VALUE` or `--post VALUE` choose the phase (mutually exclusive);
  `--percent VALUE` or `--relative VALUE` choose the unit (mutually exclusive,
  default percent). When both a phase flag and a unit flag carry a value, the unit
  flag supplies the value — so `--post 45 --relative 1.45` records 1.45 *relative*.
* `--depth CM` is the sampling depth (default 0), `--obs TEXT` is a free note.
* Each reading stores a local, naive ISO-8601 `ts_iso`.
* Readings may also be added while logging the event:
  `log add --rain 4 --minutes 30 --pre 31 --depth 10 --obs dry`.
* With no reading flags, `log note <id>` prompts for phase, value, unit, depth and
  observation.

**Pairing and delta.** `log show` pairs the *earliest* pre reading with the
*latest* post reading (by timestamp, then insertion order). If the units differ it
prints `mixed units — delta not computed`; if a phase is missing it prints
`no complete pre/post pair`; otherwise it prints the signed difference, e.g.
`+20 percent`.

## Browse the log

```bash
python3 petrichorlog.py log list           # newest first: id, date, rain, intensity, minutes
python3 petrichorlog.py log list --json    # machine-readable
python3 petrichorlog.py log show 1         # full detail incl. readings and delta
python3 petrichorlog.py log show 1 --json
```

`log show` prints the start/end/minutes, rain, the chosen and suggested intensity
names, notes, every reading with its timestamp, and the paired delta. `log show
999` exits non-zero with `petrichorlog: event 999 not found`.

## Seasonal pattern

```bash
python3 petrichorlog.py log season                # this year
python3 petrichorlog.py log season --year 2025
python3 petrichorlog.py log season --json
```

```
Season 2025 — 8 events, 125.6 mm total
──────────────────────────────────────────────────────────────────────────────
month   rain mm  bar                                        ev   int  moisture
──────────────────────────────────────────────────────────────────────────────
2025-01     0.0                                              0     —       n/a
2025-03     6.6  ██████                                      2   1.5    +16.0%
2025-06    46.0  ███████████████████████████████████████     2   4.0       n/a
2025-08    47.6  ████████████████████████████████████████    1   5.0       n/a
──────────────────────────────────────────────────────────────────────────────
moisture = mean paired change (percent unless marked rel; n/a = no pair)
```

Bars scale to the terminal width (40 columns at 80 wide) and to the wettest month
in the year; the driest month with any rain still gets one column. Months without
paired readings show `n/a`; a year with no events shows the empty-state invite.

## The passphrase lock

```bash
printf 'cenius\n' | python3 petrichorlog.py --passphrase-stdin log list          # set on first use, then unlock
printf 'wrong\n'  | python3 petrichorlog.py --passphrase-stdin log list          # petrichorlog: invalid passphrase  (exit 3)
python3 petrichorlog.py log list </dev/null                                      # petrichorlog: passphrase required  (exit 3)
python3 petrichorlog.py log list                                                 # masked prompt, 3 attempts
```

The passphrase is stored only as a salted PBKDF2-HMAC-SHA256 digest
(`algo`, `salt_b64`, `hash_b64`, `iterations`) in the store's `auth` field —
never in plain text. The first interactive run of any command on a store with
`auth: null` offers *"Set a passphrase (or blank to skip):"*. Blank skips the
lock permanently. A wrong passphrase is reported without saying which part was
wrong, and three wrong interactive attempts exit non-zero.

## Store file and failure behaviour

The store is one JSON document:

```json
{
  "version": 1,
  "next_id": 3,
  "auth": null,
  "events": [ { "id": 1, "start_iso": "2025-06-14T09:00:00", "end_iso": "…",
                "minutes": 30, "rain_mm": 4.0, "intensity": 3,
                "intensity_suggested": 2, "intensity_overridden": true,
                "notes": "", "readings": [ { "ts_iso": "…", "phase": "pre",
                "value": 31.0, "unit": "percent", "depth_cm": 10.0, "obs": "dry" } ],
                "created_iso": "…", "updated_iso": "…" } ]
}
```

Writes go to a temporary file in the same directory and are moved into place with
`os.replace`, so an interrupted write leaves the previous store intact. A store
that cannot be parsed, or whose `version` is not `1`, is never modified:
a corrupt file is renamed to `<path>.bak-<UTC>` (with `-1`, `-2` … if that name is
taken) and the command exits non-zero with one actionable line. Generated events
are append-only: only `log note` may extend an existing event.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 2 | bad flags / validation failure (`rain_mm must be >= 0`, `use --pre or --post, not both`, …) |
| 3 | passphrase missing, wrong, or three failed attempts |
| 4 | store unreadable, corrupt, unsupported version, or unwritable |
| 5 | event id not found |
