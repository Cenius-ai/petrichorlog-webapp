"""Test suite for PetrichorLog.

Covers every core feature: the versioned JSON store, the log add/list/show/note
path, the petrichor intensity scale and its deterministic suggestion, paired
soil-moisture deltas, the seasonal visualiser, flag/interactive validation, the
local passphrase lock, and corrupt-store / permission recovery.

Run with:  python3 -m pytest
"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import petrichorlog  # noqa: E402  (needs ROOT on sys.path)

CLI = ROOT / "petrichorlog.py"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def run_cli(args, store, stdin_text=None, env_extra=None, timeout=60):
    """Run the CLI in a subprocess against a scratch store."""
    env = dict(os.environ)
    env["PETRICHORLOG_STORE"] = str(store)
    env["COLUMNS"] = "80"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        input="" if stdin_text is None else stdin_text,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(Path(store).parent),
        timeout=timeout,
    )


def read_store(path):
    return json.loads(Path(path).read_text())


def make_event(
    event_id,
    start_iso,
    minutes=30,
    rain_mm=4.0,
    intensity=2,
    suggested=2,
    notes="",
    readings=None,
):
    return {
        "id": event_id,
        "start_iso": start_iso,
        "end_iso": start_iso,
        "minutes": minutes,
        "rain_mm": rain_mm,
        "intensity": intensity,
        "intensity_suggested": suggested,
        "intensity_overridden": intensity != suggested,
        "notes": notes,
        "readings": readings or [],
        "created_iso": start_iso,
        "updated_iso": start_iso,
    }


def make_reading(ts_iso, phase, value, unit="percent", depth_cm=10.0, obs=""):
    return {
        "ts_iso": ts_iso,
        "phase": phase,
        "value": value,
        "unit": unit,
        "depth_cm": depth_cm,
        "obs": obs,
    }


def write_store(path, events, auth=None):
    highest = max((int(e["id"]) for e in events), default=0)
    Path(path).write_text(
        json.dumps(
            {
                "version": 1,
                "next_id": highest + 1,
                "auth": auth,
                "events": events,
            }
        )
    )


def run_in_pty(args, feed, store, timeout=30):
    """Run the CLI attached to a pty so the interactive prompts are exercised."""
    env = dict(os.environ)
    env["PETRICHORLOG_STORE"] = str(store)
    env["COLUMNS"] = "80"
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(CLI), *args],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=str(Path(store).parent),
        env=env,
        close_fds=True,
    )
    os.close(slave)
    os.write(master, feed.encode())
    chunks = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([master], [], [], 0.3)
        if ready:
            try:
                data = os.read(master, 8192)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        elif proc.poll() is not None:
            break
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    os.close(master)
    output = b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")
    return proc.returncode, output


@pytest.fixture()
def store(tmp_path):
    return tmp_path / "petrichorlog.json"


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------
def test_help_lists_every_command(store):
    result = run_cli(["--help"], store)
    assert result.returncode == 0
    for command in ("log", "add", "list", "show", "note", "season"):
        assert command in result.stdout
    assert "--passphrase-stdin" in result.stdout


def test_version_flag(store):
    result = run_cli(["--version"], store)
    assert result.returncode == 0
    assert petrichorlog.__version__ in result.stdout


def test_bare_invocation_prints_scale_and_exits_zero(store):
    result = run_cli([], store)
    assert result.returncode == 0
    assert "Petrichor intensity scale" in result.stdout
    assert "5 — Intense" in result.stdout
    assert not store.exists()  # a bare help screen never touches the store


# --------------------------------------------------------------------------
# Store bootstrap (T1)
# --------------------------------------------------------------------------
def test_empty_store_is_created_with_the_documented_schema(store):
    result = run_cli(["log", "list"], store)
    assert result.returncode == 0
    assert result.stdout.strip() == "No events yet — add one with 'petrichorlog log add'."

    payload = read_store(store)
    assert payload == {"version": 1, "next_id": 1, "auth": None, "events": []}


def test_unsupported_store_version_is_refused_untouched(store):
    original = '{"version": 2, "next_id": 1, "auth": null, "events": []}'
    store.write_text(original)

    result = run_cli(["log", "list"], store)
    assert result.returncode != 0
    assert "unsupported store version 2; this build supports 1" in result.stderr
    assert store.read_text() == original


# --------------------------------------------------------------------------
# F1 — logging a rain event
# --------------------------------------------------------------------------
def test_add_event_in_flag_mode(store):
    result = run_cli(
        ["log", "add", "--rain", "4", "--minutes", "30", "--intensity", "3"], store
    )
    assert result.returncode == 0
    assert "Logged rain event 1" in result.stdout

    event = read_store(store)["events"][0]
    assert event["id"] == 1
    assert event["rain_mm"] == 4.0
    assert event["minutes"] == 30
    assert event["intensity"] == 3
    assert event["start_iso"] and event["end_iso"]
    assert event["created_iso"] and event["updated_iso"]
    assert event["readings"] == []


def test_start_plus_minutes_resolves_today(store):
    result = run_cli(
        ["log", "add", "--start", "09:00", "--minutes", "30", "--rain", "1"], store
    )
    assert result.returncode == 0

    event = read_store(store)["events"][0]
    now = datetime.now()
    assert event["start_iso"] == datetime.combine(
        now.date(), datetime.min.time()
    ).replace(hour=9, minute=0).isoformat()
    assert event["end_iso"] == datetime.combine(
        now.date(), datetime.min.time()
    ).replace(hour=9, minute=30).isoformat()
    assert event["minutes"] == 30


def test_end_flag_resolves_minutes_by_ceiling(store):
    result = run_cli(
        ["log", "add", "--start", "09:00", "--end", "10:30", "--rain", "2"], store
    )
    assert result.returncode == 0
    event = read_store(store)["events"][0]
    assert event["minutes"] == 90
    assert event["end_iso"].endswith("10:30:00")


def test_minutes_and_end_are_mutually_exclusive(store):
    result = run_cli(
        ["log", "add", "--minutes", "30", "--end", "14:00", "--rain", "1"], store
    )
    assert result.returncode != 0
    assert "provide either --minutes or --end, not both" in result.stderr
    assert not store.exists()


def test_end_must_be_after_start(store):
    result = run_cli(
        ["log", "add", "--start", "09:00", "--end", "08:30", "--rain", "1"], store
    )
    assert result.returncode != 0
    assert "end must be after start" in result.stderr


def test_flag_mode_error_is_exactly_one_line(store):
    result = run_cli(["log", "add", "--rain", "-1", "--minutes", "10"], store)
    assert result.returncode != 0
    assert result.stderr.strip().splitlines() == ["petrichorlog: rain_mm must be >= 0"]


# --------------------------------------------------------------------------
# F7 — validation rules
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["log", "add", "--rain", "-1", "--minutes", "10"], "rain_mm must be >= 0"),
        (["log", "add", "--minutes", "0", "--rain", "1"], "minutes must be > 0"),
        (["log", "add", "--intensity", "6", "--rain", "1", "--minutes", "10"], "intensity must be 0-5"),
        (["log", "add", "--depth", "-1", "--rain", "1", "--minutes", "10"], "depth must be >= 0"),
        (["log", "add", "--pre", "31", "--post", "45", "--rain", "1", "--minutes", "10"], "use --pre or --post, not both"),
        (["log", "add", "--percent", "31", "--relative", "1.2", "--rain", "1", "--minutes", "10"], "use --percent or --relative, not both"),
    ],
)
def test_add_validation_messages(store, args, message):
    result = run_cli(args, store)
    assert result.returncode != 0
    assert message in result.stderr
    assert not store.exists() or read_store(store)["events"] == []


def test_note_validation_messages(store):
    write_store(store, [make_event(1, "2025-06-14T09:00:00")])

    percent = run_cli(["log", "note", "1", "--pre", "101"], store)
    assert percent.returncode != 0
    assert "percent must be 0-100" in percent.stderr

    relative = run_cli(["log", "note", "1", "--relative", "-1"], store)
    assert relative.returncode != 0
    assert "relative must be > 0" in relative.stderr

    phase = run_cli(["log", "note", "1", "--pre", "31", "--post", "45"], store)
    assert phase.returncode != 0
    assert "use --pre or --post, not both" in phase.stderr


def test_interactive_validation_reprompts_with_the_field_name(store):
    feed = "09:00\n30\nabc\n4\n\n\n\n"
    result = run_cli(["log", "add"], store, stdin_text=feed)
    assert result.returncode == 0
    assert "rain_mm: enter a number" in result.stderr
    assert "Logged rain event 1" in result.stdout
    assert read_store(store)["events"][0]["rain_mm"] == 4.0


def test_interactive_wizard_saves_every_answer(store):
    feed = "09:00\n30\n8.5\n\nstorm\npost\n44\npercent\n10\nsoaked\n"
    result = run_cli(["log", "add"], store, stdin_text=feed)
    assert result.returncode == 0

    event = read_store(store)["events"][0]
    assert event["start_iso"].endswith("09:00:00")
    assert event["minutes"] == 30
    assert event["rain_mm"] == 8.5
    assert event["intensity"] == event["intensity_suggested"]
    assert event["notes"] == "storm"
    reading = event["readings"][0]
    assert reading["phase"] == "post"
    assert reading["value"] == 44.0
    assert reading["unit"] == "percent"
    assert reading["depth_cm"] == 10.0
    assert reading["obs"] == "soaked"


# --------------------------------------------------------------------------
# F2 — intensity scale and deterministic suggestion
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("rain_mm", "minutes", "expected"),
    [
        (0.0, 0, 0),
        (0.0, 11, 0),
        (0.4, 8, 0),
        (2.0, 10, 1),
        (6.2, 35, 3),
        (12.0, 45, 3),
        (21.5, 95, 5),
        (47.6, 210, 5),
    ],
)
def test_suggestion_formula(rain_mm, minutes, expected):
    assert petrichorlog.suggest_intensity(rain_mm, minutes) == expected


def test_suggested_intensity_is_shown_and_saved(store):
    result = run_cli(["log", "add", "--rain", "12", "--minutes", "45"], store)
    assert result.returncode == 0
    assert (
        "suggested  3 — Earthy: clear petrichor, wet soil and geosmin aroma"
        in result.stdout
    )

    event = read_store(store)["events"][0]
    assert event["intensity"] == 3
    assert event["intensity_suggested"] == 3
    assert event["intensity_overridden"] is False


def test_override_is_recorded_and_shown_next_to_the_suggestion(store):
    run_cli(
        ["log", "add", "--rain", "2", "--minutes", "10", "--intensity", "5"], store
    )
    event = read_store(store)["events"][0]
    assert event["intensity"] == 5
    assert event["intensity_suggested"] == 1
    assert event["intensity_overridden"] is True

    detail = run_cli(["log", "show", "1"], store)
    assert "5 — Intense" in detail.stdout
    assert "1 — Faint" in detail.stdout


def test_interactive_intensity_prompt_lists_all_six_levels(store):
    feed = "09:00\n30\n2\n\n\n\n"
    result = run_cli(["log", "add"], store, stdin_text=feed)
    assert result.returncode == 0
    for level, name, descriptor in petrichorlog.INTENSITY_SCALE:
        assert f"{level} — {name}: {descriptor}" in result.stderr


# --------------------------------------------------------------------------
# F3 — soil moisture readings
# --------------------------------------------------------------------------
def test_add_event_with_pre_reading_flags(store):
    result = run_cli(
        [
            "log", "add", "--rain", "4", "--minutes", "30",
            "--pre", "31", "--depth", "10", "--obs", "dry",
        ],
        store,
    )
    assert result.returncode == 0

    reading = read_store(store)["events"][0]["readings"][0]
    assert reading["phase"] == "pre"
    assert reading["value"] == 31.0
    assert reading["unit"] == "percent"
    assert reading["depth_cm"] == 10.0
    assert reading["obs"] == "dry"
    assert datetime.fromisoformat(reading["ts_iso"]).tzinfo is None


def test_note_appends_relative_reading(store):
    write_store(store, [make_event(1, "2025-06-14T09:00:00")])
    result = run_cli(
        [
            "log", "note", "1", "--post", "45", "--relative", "1.45",
            "--depth", "10", "--obs", "damp",
        ],
        store,
    )
    assert result.returncode == 0

    reading = read_store(store)["events"][0]["readings"][0]
    assert reading["phase"] == "post"
    assert reading["unit"] == "relative"
    assert reading["value"] == 1.45
    assert reading["obs"] == "damp"


def test_note_interactive_prompt_appends_reading(store):
    write_store(store, [make_event(1, "2025-06-14T09:00:00")])
    feed = "pre\n30\npercent\n5\ncrusty\n"
    result = run_cli(["log", "note", "1"], store, stdin_text=feed)
    assert result.returncode == 0
    assert "Added pre reading to event 1" in result.stdout

    reading = read_store(store)["events"][0]["readings"][0]
    assert (reading["phase"], reading["value"], reading["unit"]) == ("pre", 30.0, "percent")
    assert reading["obs"] == "crusty"


# --------------------------------------------------------------------------
# F5/F6 — pairing, delta, list and detail views
# --------------------------------------------------------------------------
def test_delta_pairs_earliest_pre_with_latest_post(store):
    write_store(
        store,
        [
            make_event(
                1,
                "2025-06-14T09:00:00",
                readings=[
                    make_reading("2025-06-14T09:00:00", "pre", 30.0),
                    make_reading("2025-06-14T10:00:00", "post", 45.0),
                    make_reading("2025-06-14T11:00:00", "post", 50.0),
                ],
            )
        ],
    )
    result = run_cli(["log", "show", "1"], store)
    assert result.returncode == 0
    assert "+20 percent" in result.stdout


def test_delta_reports_missing_pair(store):
    write_store(
        store,
        [
            make_event(
                1,
                "2025-06-14T09:00:00",
                readings=[make_reading("2025-06-14T09:00:00", "pre", 30.0)],
            )
        ],
    )
    result = run_cli(["log", "show", "1"], store)
    assert "no complete pre/post pair" in result.stdout


def test_delta_reports_mixed_units_and_still_lists_both_readings(store):
    write_store(
        store,
        [
            make_event(
                1,
                "2025-06-14T09:00:00",
                readings=[
                    make_reading("2025-06-14T09:00:00", "pre", 30.0, "percent"),
                    make_reading("2025-06-14T10:00:00", "post", 1.3, "relative"),
                ],
            )
        ],
    )
    result = run_cli(["log", "show", "1"], store)
    assert "mixed units — delta not computed" in result.stdout
    assert "30 percent" in result.stdout
    assert "1.3 relative" in result.stdout


def test_pairing_breaks_ties_by_insertion_order():
    readings = [
        make_reading("2025-06-14T09:00:00", "pre", 10.0),
        make_reading("2025-06-14T09:00:00", "pre", 20.0),
        make_reading("2025-06-14T11:00:00", "post", 40.0),
        make_reading("2025-06-14T11:00:00", "post", 50.0),
    ]
    delta, unit = petrichorlog.moisture_delta(readings)
    assert (delta, unit) == (40.0, "percent")  # earliest pre 10, latest post 50


def test_list_is_newest_first_with_summary_columns(store):
    run_cli(["log", "add", "--start", "2025-01-05T08:00", "--rain", "3", "--minutes", "20"], store)
    run_cli(["log", "add", "--start", "2025-02-11T18:30", "--rain", "9.5", "--minutes", "65"], store)

    result = run_cli(["log", "list"], store)
    assert result.returncode == 0
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("2")]
    assert "2025-02-11" in lines[0]
    assert "9.5 mm" in lines[0]
    assert result.stdout.index("2025-02-11") < result.stdout.index("2025-01-05")
    assert "2 events logged, newest first." in result.stdout


def test_show_detail_and_not_found(store):
    write_store(
        store,
        [make_event(1, "2025-06-14T09:00:00", notes="warm ground", rain_mm=6.5)],
    )
    result = run_cli(["log", "show", "1"], store)
    assert result.returncode == 0
    for fragment in ("Event 1", "2025-06-14T09:00:00", "minutes", "6.5 mm", "warm ground"):
        assert fragment in result.stdout

    missing = run_cli(["log", "show", "999"], store)
    assert missing.returncode != 0
    assert "event 999 not found" in missing.stderr


def test_json_outputs_are_machine_readable(store):
    run_cli(["log", "add", "--rain", "4", "--minutes", "30"], store)

    listing = run_cli(["log", "list", "--json"], store)
    payload = json.loads(listing.stdout)
    assert payload["count"] == 1 and payload["events"][0]["id"] == 1

    detail = run_cli(["log", "show", "1", "--json"], store)
    assert json.loads(detail.stdout)["rain_mm"] == 4.0

    season = run_cli(["log", "season", "--json"], store)
    assert len(json.loads(season.stdout)["months"]) == 12


# --------------------------------------------------------------------------
# F4 — seasonal pattern visualiser
# --------------------------------------------------------------------------
def test_season_aggregates_months(store):
    run_cli(["log", "add", "--start", "2025-01-05T08:00", "--rain", "30", "--minutes", "60", "--intensity", "2"], store)
    run_cli(["log", "add", "--start", "2025-01-19T08:00", "--rain", "10", "--minutes", "30", "--intensity", "4"], store)
    run_cli(["log", "add", "--start", "2025-02-02T08:00", "--rain", "5", "--minutes", "20", "--intensity", "1"], store)

    result = run_cli(["log", "season", "--year", "2025"], store)
    assert result.returncode == 0
    january = next(line for line in result.stdout.splitlines() if line.startswith("2025-01"))
    february = next(line for line in result.stdout.splitlines() if line.startswith("2025-02"))
    assert "40.0" in january and " 2 " in january and "3.0" in january
    assert "5.0" in february and "1 " in february
    month_lines = [l for l in result.stdout.splitlines() if l.startswith("2025-")]
    assert len(month_lines) == 12
    assert all("n/a" in line for line in month_lines)  # no paired readings anywhere


def test_season_empty_state(store):
    result = run_cli(["log", "season", "--year", "2025"], store)
    assert result.returncode == 0
    assert "No events for 2025 yet — add one with 'petrichorlog log add'." in result.stdout


def test_season_reports_monthly_delta_and_na(store):
    run_cli(["log", "add", "--start", "2025-03-02T08:00", "--rain", "12", "--minutes", "40"], store)
    run_cli(["log", "note", "1", "--pre", "30", "--depth", "10"], store)
    run_cli(["log", "note", "1", "--post", "45", "--depth", "10"], store)
    run_cli(["log", "add", "--start", "2025-04-02T08:00", "--rain", "6", "--minutes", "20"], store)

    result = run_cli(["log", "season", "--year", "2025"], store)
    march = next(line for line in result.stdout.splitlines() if line.startswith("2025-03"))
    april = next(line for line in result.stdout.splitlines() if line.startswith("2025-04"))
    assert "+15.0%" in march
    assert april.strip().endswith("n/a")


def test_season_bars_scale_to_the_terminal_width(monkeypatch):
    class FixedSize:
        columns = 80
        lines = 24

    monkeypatch.setattr(
        petrichorlog.shutil, "get_terminal_size", lambda fallback=None: FixedSize()
    )
    events = [
        make_event(1, "2025-01-05T08:00:00", rain_mm=100.0),
        make_event(2, "2025-02-05T08:00:00", rain_mm=1.0),
    ]
    rendered = petrichorlog.render_season(events, 2025)
    january = next(line for line in rendered.splitlines() if line.startswith("2025-01"))
    february = next(line for line in rendered.splitlines() if line.startswith("2025-02"))
    assert january.count("█") == 40
    assert february.count("█") == 1


# --------------------------------------------------------------------------
# F6 — local passphrase lock
# --------------------------------------------------------------------------
def test_passphrase_stdin_sets_then_unlocks(store):
    set_result = run_cli(
        ["--passphrase-stdin", "log", "add", "--rain", "2", "--minutes", "10"],
        store,
        stdin_text="cenius\n",
    )
    assert set_result.returncode == 0

    auth = read_store(store)["auth"]
    assert auth["algo"] == "pbkdf2_hmac_sha256"
    assert auth["iterations"] >= 100_000
    assert auth["salt_b64"] and auth["hash_b64"]
    assert "cenius" not in json.dumps(auth)

    unlocked = run_cli(["--passphrase-stdin", "log", "list"], store, stdin_text="cenius\n")
    assert unlocked.returncode == 0
    assert "1 event logged" in unlocked.stdout

    rejected = run_cli(["log", "list", "--passphrase-stdin"], store, stdin_text="nope\n")
    assert rejected.returncode != 0
    assert "invalid passphrase" in rejected.stderr


def test_passphrase_required_when_stdin_is_empty(store):
    run_cli(["--passphrase-stdin", "log", "list"], store, stdin_text="cenius\n")

    result = run_cli(["--passphrase-stdin", "log", "list"], store, stdin_text="")
    assert result.returncode != 0
    assert "passphrase required" in result.stderr


def test_locked_store_without_passphrase_and_without_tty(store):
    run_cli(["--passphrase-stdin", "log", "list"], store, stdin_text="cenius\n")

    result = run_cli(["log", "list"], store)
    assert result.returncode != 0
    assert "passphrase required" in result.stderr


def test_hash_and_verify_round_trip():
    auth = petrichorlog.hash_passphrase("cenius", salt=b"0123456789abcdef")
    assert petrichorlog.verify_passphrase(auth, "cenius") is True
    assert petrichorlog.verify_passphrase(auth, "Cenius") is False


def test_interactive_passphrase_set_and_unlock(store):
    code, output = run_in_pty(["log", "list"], "cenius\ncenius\n", store)
    assert code == 0
    assert "Set a passphrase (or blank to skip):" in output
    assert read_store(store)["auth"]["algo"] == "pbkdf2_hmac_sha256"

    code, output = run_in_pty(["log", "list"], "cenius\n", store)
    assert code == 0
    assert "Passphrase:" in output
    assert "No events yet" in output


def test_interactive_passphrase_gives_three_attempts(store):
    run_cli(["--passphrase-stdin", "log", "list"], store, stdin_text="cenius\n")

    code, output = run_in_pty(["log", "list"], "alpha\nbravo\ncharlie\n", store)
    assert code != 0
    assert "invalid passphrase" in output


def test_blank_passphrase_skips_the_lock(store):
    code, output = run_in_pty(["log", "list"], "\n", store)
    assert code == 0
    assert read_store(store)["auth"] is None


# --------------------------------------------------------------------------
# F8 — corrupt store and permission recovery
# --------------------------------------------------------------------------
def test_corrupt_store_is_backed_up_and_left_alone(store):
    broken = "{ this is not json"
    store.write_text(broken)

    result = run_cli(["log", "list"], store)
    assert result.returncode != 0
    assert f"store corrupted at {store}" in result.stderr
    assert "left unchanged" in result.stderr
    assert "fix or delete to continue" in result.stderr

    backups = list(store.parent.glob("petrichorlog.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == broken

    # A second corruption in the same UTC second must not overwrite the backup.
    store.write_text(broken)
    again = run_cli(["log", "list"], store)
    assert again.returncode != 0
    assert len(list(store.parent.glob("petrichorlog.json.bak-*"))) == 2


def test_unique_backup_path_adds_a_numeric_suffix(tmp_path):
    path = tmp_path / "petrichorlog.json"
    stamp = "20250614T090000Z"
    (tmp_path / f"petrichorlog.json.bak-{stamp}").write_text("first")

    assert (
        petrichorlog.unique_backup_path(str(path), stamp)
        == str(tmp_path / f"petrichorlog.json.bak-{stamp}-1")
    )


def test_permission_denied_reading_is_reported(store, monkeypatch):
    store.write_text(json.dumps({"version": 1, "next_id": 1, "auth": None, "events": []}))
    real_open = open

    def deny(path, *args, **kwargs):
        if str(path) == str(store):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny)
    with pytest.raises(petrichorlog.CliError) as excinfo:
        petrichorlog.load_store(str(store))
    assert "permission denied reading" in str(excinfo.value)
    assert "check file permissions" in str(excinfo.value)
    assert excinfo.value.code == petrichorlog.EXIT_STORE


def test_permission_denied_write_leaves_the_original(store, monkeypatch):
    store.write_text('{"version": 1, "next_id": 1, "auth": null, "events": []}')
    original = store.read_text()

    def deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(petrichorlog.tempfile, "mkstemp", deny)
    with pytest.raises(petrichorlog.CliError) as excinfo:
        petrichorlog.save_store(str(store), petrichorlog.fresh_store())
    assert "could not write" in str(excinfo.value)
    assert "original unchanged" in str(excinfo.value)
    assert store.read_text() == original


def test_failed_atomic_replace_keeps_the_original_and_cleans_up(store, monkeypatch):
    write_store(store, [make_event(1, "2025-06-14T09:00:00")])
    original = store.read_text()

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(petrichorlog.os, "replace", boom)
    with pytest.raises(petrichorlog.CliError) as excinfo:
        petrichorlog.save_store(str(store), petrichorlog.fresh_store())
    assert "could not write" in str(excinfo.value)
    assert store.read_text() == original
    assert list(Path(store).parent.glob(".petrichorlog-*.tmp")) == []


def test_add_event_round_trips_through_disk(store):
    run_cli(["log", "add", "--rain", "4", "--minutes", "30"], store)
    assert "4.0 mm" in run_cli(["log", "list"], store).stdout
    store.write_text(store.read_text())  # rewrite the file as-is and re-read it
    assert read_store(store)["events"][0]["rain_mm"] == 4.0


# --------------------------------------------------------------------------
# Dates, defaults and the season year filter
# --------------------------------------------------------------------------
def test_season_defaults_to_the_current_year(store):
    year = date.today().year
    run_cli(["log", "add", "--start", f"{year}-05-04T08:00", "--rain", "7", "--minutes", "25"], store)
    result = run_cli(["log", "season"], store)
    assert result.returncode == 0
    assert f"Season {year}" in result.stdout


def test_intensity_label_helpers():
    assert petrichorlog.intensity_label(3) == "3 — Earthy"
    assert "geosmin" in petrichorlog.intensity_line(3)
    assert petrichorlog.format_delta(20.0, "percent") == "+20 percent"
    assert petrichorlog.format_delta(-5.5, "percent") == "-5.5 percent"
