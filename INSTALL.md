# Installing and running PetrichorLog

PetrichorLog is a single-file Python 3 CLI. There is no database, no service and
no configuration file to write — the JSON store creates itself on first use.

## 1. Prerequisites (system level, install yourself if missing)

| Requirement | Why | Check |
| --- | --- | --- |
| Python 3.10+ (3.11 recommended) | Runs the CLI and the test suite | `python3 --version` |

Nothing else is required. Do **not** install a database or a web server, and no
`apt`/`brew` package is needed: PetrichorLog imports only the standard library.
`install.sh` deliberately never touches system packages.

## 2. Install the project dependencies (one command, then it exits)

```bash
bash install.sh
```

This upgrades `pip`/`setuptools`/`wheel`, installs the pinned test runner from
`requirements.txt`, installs the CLI itself (`pip install -e .`) so that the
`petrichorlog` console script lands on your `PATH`, and prints the imported
version as a self-check. It is idempotent — running it twice is fine — and it
exits without starting anything long running.

If you would rather not install anything at all, skip this step: the tool runs
directly with `python3 petrichorlog.py`. Only the test suite needs `pytest`.

## 3. Seed the demo data (its own step, before you run anything)

```bash
bash demo.sh
```

`demo.sh` builds a scratch store at `.petrichorlog-demo/petrichorlog.json` using
the CLI's own commands: eight rain events across a season, seven soil-moisture
readings, then the list/detail/season views, the flag-mode failure paths, the
corrupt-store backup and the passphrase lock. It is non-interactive, safe to
re-run (it starts from an empty scratch store) and exits 0.

The standard demo passphrase used for the locked store is `cenius`; see the
"Demo accounts" table in [README.md](README.md).

To seed the *default* store instead, just log events into it directly:

```bash
python3 petrichorlog.py log add --start "$(date +%Y)-03-19T17:45" --rain 6.2 --minutes 35 --notes "warm ground, smell arrived late"
python3 petrichorlog.py log note 1 --pre 28 --depth 10 --obs "dry crust"
python3 petrichorlog.py log note 1 --post 44 --depth 10 --obs "soaked through"
```

## 4. Run it

```bash
python3 petrichorlog.py --help                       # usage, examples, the intensity scale
python3 petrichorlog.py log add --rain 4 --minutes 30
python3 petrichorlog.py log list
python3 petrichorlog.py log show 1
python3 petrichorlog.py log season --year 2025
```

After step 2 you can drop the `python3 petrichorlog.py` prefix and type
`petrichorlog` instead.

Environment variables (all optional — the in-code defaults are what run when
nothing is set, so a fresh checkout works with no `.env` at all):

| Variable | Default | Meaning |
| --- | --- | --- |
| `PETRICHORLOG_STORE` | `petrichorlog.json` | Path to the JSON store file |
| `COLUMNS` | terminal width, else 80 | Width the season bars scale to |

See [.env.example](.env.example) for the same list in dotenv form.

## 5. Test

```bash
python3 -m pytest
```

The suite runs offline against temporary stores and covers the store schema,
validation rules, the add/list/show/note path, the intensity scale and its
formula, paired moisture deltas, the seasonal visualiser, the passphrase lock and
corrupt/permission recovery. No fixtures, services or network are needed.

## Uninstalling

Delete the tool directory and, if you like, the store next to it
(`petrichorlog.json`). Nothing else was installed except pip packages, which you
can remove with `python3 -m pip uninstall petrichorlog pytest`.
