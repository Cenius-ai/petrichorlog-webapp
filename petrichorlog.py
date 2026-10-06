#!/usr/bin/env python3
"""PetrichorLog — a local rain-event log with soil-moisture notes.

Records rain events (start/end, millimetres, petrichor intensity 0-5), lets you
append pre/post soil-moisture readings with an observation, and visualises the
seasonal pattern month by month.  Everything lives in one versioned JSON
document next to you; no network, no accounts, standard library only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

__version__ = "1.0.0"

PROG = "petrichorlog"
STORE_VERSION = 1
DEFAULT_STORE = "petrichorlog.json"
STORE_ENV = "PETRICHORLOG_STORE"

PBKDF2_ALGO = "pbkdf2_hmac_sha256"
PBKDF2_ITERATIONS = 120_000
MAX_PASSPHRASE_ATTEMPTS = 3

MAX_BAR_WIDTH = 40
MIN_BAR_WIDTH = 8
SEASON_FIXED_COLUMNS = 32

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_STORE = 4
EXIT_NOT_FOUND = 5

# --------------------------------------------------------------------------
# Committed design tokens (see DESIGN.md): calm-precise, terminal dark, one
# accent.  The CLI itself is deliberately monochrome (the plan excludes colour
# output) and aligns every value into fixed terminal columns.
# --------------------------------------------------------------------------
THEME = {
    "accent_oklch": "oklch(0.58 0.12 198)",
    "accent_hex": "#008f94",
    "surface": "terminal-dark",
    "type": "monospace",
    "rule": "─",
    "bar": "█",
}

# Petrichor intensity scale: (level, name, sensory descriptor)
INTENSITY_SCALE: tuple[tuple[int, str, str], ...] = (
    (0, "None", "no petrichor, dry air, no earthy smell"),
    (1, "Faint", "barely detectable hint of damp dust if you inhale deeply"),
    (2, "Mild", "light earthy scent noticeable when close to soil"),
    (3, "Earthy", "clear petrichor, wet soil and geosmin aroma"),
    (4, "Strong", "prominent petrichor with ozone and fresh rain on warm ground"),
    (5, "Intense", "overpowering geosmin, almost metallic"),
)

INTENSITY_NAMES = {level: name for level, name, _ in INTENSITY_SCALE}
INTENSITY_DESCRIPTORS = {level: descriptor for level, _, descriptor in INTENSITY_SCALE}

TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class CliError(Exception):
    """Fatal error: exactly one actionable line on stderr, non-zero exit."""

    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


class ValidationError(CliError):
    """A recoverable field error: interactive mode re-prompts, flags exit."""


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def intensity_label(level: int) -> str:
    return f"{level} — {INTENSITY_NAMES.get(level, 'Unknown')}"


def intensity_line(level: int) -> str:
    return (
        f"{level} — {INTENSITY_NAMES.get(level, 'Unknown')}: "
        f"{INTENSITY_DESCRIPTORS.get(level, '')}"
    )


def format_mm(value: float) -> str:
    return f"{value:.1f} mm"


def format_depth(value: float) -> str:
    return f"{value:g} cm"


def format_delta(delta: float, unit: str) -> str:
    return f"{delta:+g} {unit}"


def local_now() -> datetime:
    return datetime.now().replace(microsecond=0)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Store: load / save / recover
# --------------------------------------------------------------------------
def fresh_store() -> dict:
    return {"version": STORE_VERSION, "next_id": 1, "auth": None, "events": []}


def store_path(args: argparse.Namespace) -> str:
    return args.store or os.environ.get(STORE_ENV) or DEFAULT_STORE


def unique_backup_path(path: str, stamp: str) -> str:
    """<path>.bak-<stamp>, with a numeric suffix so no backup is overwritten."""
    base = f"{path}.bak-{stamp}"
    candidate = base
    suffix = 1
    while os.path.exists(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _backup_corrupt(path: str) -> str:
    backup = unique_backup_path(path, utc_stamp())
    try:
        os.rename(path, backup)
    except PermissionError:
        raise CliError(
            f"permission denied writing {path}; check file permissions", EXIT_STORE
        ) from None
    except OSError as exc:
        raise CliError(
            f"could not move {path} aside; {exc.strerror or exc}", EXIT_STORE
        ) from None
    return backup


def _corrupt(path: str) -> CliError:
    try:
        backup = _backup_corrupt(path)
    except CliError as exc:
        return exc
    return CliError(
        f"store corrupted at {path}; left unchanged; backup at {backup}; "
        "fix or delete to continue",
        EXIT_STORE,
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _event_is_wellformed(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    if not _is_int(event.get("id")):
        return False
    if not isinstance(event.get("start_iso"), str):
        return False
    if not isinstance(event.get("notes", ""), str):
        return False
    if not isinstance(event.get("rain_mm"), (int, float)):
        return False
    if not _is_int(event.get("intensity")):
        return False
    readings = event.get("readings", [])
    if not isinstance(readings, list):
        return False
    return all(
        isinstance(reading, dict) and reading.get("phase") in ("pre", "post")
        for reading in readings
    )


def load_store(path: str) -> tuple[dict, bool]:
    """Return (store, existed).  Raises CliError for unreadable/corrupt stores."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return fresh_store(), False
    except PermissionError:
        raise CliError(
            f"permission denied reading {path}; check file permissions", EXIT_STORE
        ) from None
    except IsADirectoryError:
        raise CliError(f"cannot read {path}; it is a directory", EXIT_STORE) from None
    except OSError as exc:
        raise CliError(
            f"could not read {path}; {exc.strerror or exc}", EXIT_STORE
        ) from None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise _corrupt(path) from None

    if not isinstance(data, dict):
        raise _corrupt(path)
    version = data.get("version")
    if not _is_int(version):
        raise _corrupt(path)
    if version != STORE_VERSION:
        raise CliError(
            f"unsupported store version {version}; this build supports {STORE_VERSION}",
            EXIT_STORE,
        )
    events = data.get("events")
    if not isinstance(events, list) or not all(_event_is_wellformed(e) for e in events):
        raise _corrupt(path)

    next_id = data.get("next_id")
    highest = max((int(event["id"]) for event in events), default=0)
    if not _is_int(next_id) or next_id <= highest:
        next_id = highest + 1

    auth = data.get("auth")
    if auth is not None and not isinstance(auth, dict):
        raise _corrupt(path)

    return (
        {"version": STORE_VERSION, "next_id": next_id, "auth": auth, "events": events},
        True,
    )


def save_store(path: str, store: dict) -> None:
    """Atomic write: temp file in the same directory, then os.replace."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        handle_fd, temp_path = tempfile.mkstemp(
            prefix=".petrichorlog-", suffix=".tmp", dir=directory
        )
    except OSError:
        raise CliError(
            f"could not write {path}; original unchanged", EXIT_STORE
        ) from None
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise CliError(
            f"could not write {path}; original unchanged", EXIT_STORE
        ) from None


def find_event(store: dict, event_id: int) -> dict:
    for event in store["events"]:
        if event["id"] == event_id:
            return event
    raise CliError(f"event {event_id} not found", EXIT_NOT_FOUND)


# --------------------------------------------------------------------------
# Passphrase lock (PBKDF2-HMAC-SHA256, local only)
# --------------------------------------------------------------------------
def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def hash_passphrase(passphrase: str, salt: bytes | None = None) -> dict:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return {
        "algo": PBKDF2_ALGO,
        "salt_b64": _b64encode(salt),
        "hash_b64": _b64encode(digest),
        "iterations": PBKDF2_ITERATIONS,
    }


def verify_passphrase(auth: dict, passphrase: str) -> bool:
    try:
        algo = auth["algo"]
        salt = _b64decode(auth["salt_b64"])
        expected = _b64decode(auth["hash_b64"])
        iterations = int(auth["iterations"])
    except (KeyError, TypeError, ValueError):
        raise CliError(
            "store auth record is malformed; delete it to reset the lock", EXIT_STORE
        ) from None
    if algo != PBKDF2_ALGO or iterations < 1:
        raise CliError(
            "store auth record is malformed; delete it to reset the lock", EXIT_STORE
        )
    digest = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def _read_masked_line() -> str | None:
    """One stdin line with echo suppressed when a real terminal is attached."""
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        return line if line != "" else None
    try:
        import termios

        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        masked = termios.tcgetattr(fd)
        masked[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, masked)
        try:
            line = sys.stdin.readline()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
            sys.stderr.write("\n")
            sys.stderr.flush()
    except (ImportError, OSError, ValueError):
        line = sys.stdin.readline()
    return line if line != "" else None


def read_secret(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = _read_masked_line()
    if line is None:
        raise CliError(
            "passphrase required; use --passphrase-stdin for non-interactive use",
            EXIT_AUTH,
        )
    return line.rstrip("\n")


def unlock(store: dict, passphrase_stdin: bool) -> tuple[dict, bool]:
    """Return (store, changed) — sets or checks the local passphrase."""
    if store["auth"] is None:
        if passphrase_stdin:
            line = sys.stdin.readline()
            passphrase = line.rstrip("\n")
            if line == "" or passphrase == "":
                raise CliError("passphrase required", EXIT_AUTH)
            store["auth"] = hash_passphrase(passphrase)
            return store, True
        if not sys.stdin.isatty():
            return store, False
        while True:
            first = read_secret("Set a passphrase (or blank to skip): ")
            if first == "":
                return store, False
            second = read_secret("Confirm passphrase: ")
            if first != second:
                sys.stderr.write("passphrase: confirmation did not match, try again\n")
                continue
            store["auth"] = hash_passphrase(first)
            return store, True

    if passphrase_stdin:
        line = sys.stdin.readline()
        passphrase = line.rstrip("\n")
        if line == "" or passphrase == "":
            raise CliError("passphrase required", EXIT_AUTH)
        if not verify_passphrase(store["auth"], passphrase):
            raise CliError("invalid passphrase", EXIT_AUTH)
        return store, False

    if not sys.stdin.isatty():
        raise CliError(
            "passphrase required; use --passphrase-stdin for non-interactive use",
            EXIT_AUTH,
        )

    for attempt in range(1, MAX_PASSPHRASE_ATTEMPTS + 1):
        entered = read_secret("Passphrase: ")
        if verify_passphrase(store["auth"], entered):
            return store, False
        remaining = MAX_PASSPHRASE_ATTEMPTS - attempt
        if remaining:
            sys.stderr.write(
                f"invalid passphrase "
                f"({remaining} attempt{'s' if remaining != 1 else ''} left)\n"
            )
    raise CliError("invalid passphrase", EXIT_AUTH)


def open_store(args: argparse.Namespace, create: bool = True) -> tuple[dict, str]:
    path = store_path(args)
    store, existed = load_store(path)
    store, changed = unlock(store, bool(args.passphrase_stdin))
    if changed or (create and not existed):
        save_store(path, store)
    return store, path


# --------------------------------------------------------------------------
# Intensity suggestion
# --------------------------------------------------------------------------
def suggest_intensity(rain_mm: float, minutes: int) -> int:
    """score = (rain_mm / 10) + (minutes / 60), mapped onto the 0-5 scale."""
    score = (rain_mm / 10.0) + (minutes / 60.0)
    if score < 0.2:
        return 0
    if score < 0.5:
        return 1
    if score < 1.0:
        return 2
    if score < 2.0:
        return 3
    if score < 3.0:
        return 4
    return 5


# --------------------------------------------------------------------------
# Validation primitives
# --------------------------------------------------------------------------
def parse_number(text: str, field: str) -> float:
    stripped = text.strip()
    if not stripped:
        raise ValidationError(f"{field}: enter a number")
    try:
        return float(stripped)
    except ValueError:
        raise ValidationError(f"{field}: enter a number") from None


def check_rain(value: float) -> float:
    if value < 0:
        raise ValidationError("rain_mm must be >= 0")
    return value


def check_minutes(value: int) -> int:
    if value <= 0:
        raise ValidationError("minutes must be > 0")
    return value


def check_intensity(value: float) -> int:
    if value != int(value) or not 0 <= int(value) <= 5:
        raise ValidationError("intensity must be 0-5")
    return int(value)


def check_percent(value: float) -> float:
    if not 0 <= value <= 100:
        raise ValidationError("percent must be 0-100")
    return value


def check_relative(value: float) -> float:
    if value <= 0:
        raise ValidationError("relative must be > 0")
    return value


def check_depth(value: float) -> float:
    if value < 0:
        raise ValidationError("depth must be >= 0")
    return value


def parse_start(raw: str, now: datetime) -> datetime:
    text = raw.strip()
    if not text:
        return now
    match = TIME_RE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValidationError("start: enter a time between 00:00 and 23:59")
        return datetime.combine(now.date(), datetime.min.time()).replace(
            hour=hour, minute=minute
        )
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            "start: enter HH:MM or ISO-8601 such as 2025-06-14T09:00"
        ) from None


def parse_end(raw: str, start: datetime) -> datetime:
    """HH:MM lands on the same local day as start; ISO-8601 may be any later day."""
    text = raw.strip()
    match = TIME_RE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValidationError("end: enter a time between 00:00 and 23:59")
        candidate = datetime.combine(start.date(), datetime.min.time()).replace(
            hour=hour, minute=minute
        )
    else:
        try:
            candidate = datetime.fromisoformat(text)
        except ValueError:
            raise ValidationError(
                "end: enter HH:MM or ISO-8601 such as 2025-06-14T10:30"
            ) from None
    if candidate <= start:
        raise ValidationError("end must be after start")
    return candidate


def minutes_between(start: datetime, end: datetime) -> int:
    seconds = (end - start).total_seconds()
    return max(1, int(-(-seconds // 60)))


# --------------------------------------------------------------------------
# Interactive prompts
# --------------------------------------------------------------------------
def ask(prompt: str, default: str | None = None) -> str:
    label = f"{prompt} [{default}]: " if default is not None else f"{prompt}: "
    sys.stderr.write(label)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if line == "":
        raise CliError(
            f"input ended at '{prompt}'; pass flags for non-interactive use",
            EXIT_USAGE,
        )
    value = line.strip()
    if value == "" and default is not None:
        return default
    return value


def prompt_value(prompt: str, field: str, check, default: float | None = None):
    """Re-prompt on a recoverable field error until the value validates."""
    while True:
        raw = ask(prompt, None if default is None else f"{default:g}")
        try:
            return check(parse_number(raw, field))
        except ValidationError as exc:
            sys.stderr.write(f"{exc}\n")


def prompt_choice(prompt: str, options: tuple[str, ...], default: str) -> str:
    while True:
        raw = ask(prompt, default)
        candidate = raw.strip().lower() or default
        if candidate in options:
            return candidate
        sys.stderr.write(f"{prompt}: enter one of {', '.join(options)}\n")


def prompt_reading(prompt_phase: bool = True, default_phase: str = "pre") -> dict:
    phase = (
        prompt_choice("Reading phase (pre/post)", ("pre", "post"), default_phase)
        if prompt_phase
        else default_phase
    )
    while True:
        value = parse_number(ask("Value", "0"), "value")
        unit = prompt_choice("Unit (percent/relative)", ("percent", "relative"), "percent")
        try:
            value = check_percent(value) if unit == "percent" else check_relative(value)
        except ValidationError as exc:
            sys.stderr.write(f"{exc}\n")
            continue
        break
    depth = prompt_value("Depth in cm", "depth", check_depth, 0.0)
    obs = ask("Observation (optional)", "")
    return {
        "ts_iso": local_now().isoformat(),
        "phase": phase,
        "value": round(value, 3),
        "unit": unit,
        "depth_cm": round(depth, 3),
        "obs": obs,
    }


def prompt_event_fields() -> dict:
    now = local_now()
    while True:
        try:
            start = parse_start(ask("Start (HH:MM or ISO-8601, blank for now)", ""), now)
            break
        except ValidationError as exc:
            sys.stderr.write(f"{exc}\n")

    minutes: int | None = None
    end: datetime = start
    while True:
        raw = ask(
            "Duration in minutes (blank to give an end time instead)", ""
        ).strip()
        if raw:
            try:
                minutes = check_minutes(int(round(parse_number(raw, "minutes"))))
            except ValidationError as exc:
                sys.stderr.write(f"{exc}\n")
                continue
            end = start + timedelta(minutes=minutes)
            break
        while True:
            try:
                end = parse_end(ask("End (HH:MM or ISO-8601)", ""), start)
                break
            except ValidationError as exc:
                sys.stderr.write(f"{exc}\n")
        minutes = minutes_between(start, end)
        break

    rain = prompt_value("Rain in mm", "rain_mm", check_rain, 0.0)
    suggested = suggest_intensity(rain, minutes)
    render_scale(sys.stderr)
    sys.stderr.write(f"Suggested intensity: {intensity_line(suggested)}\n")
    intensity = prompt_value(
        "Intensity 0-5", "intensity", check_intensity, float(suggested)
    )
    notes = ask("Notes (optional)", "")
    readings: list[dict] = []
    answer = ask(
        "Add a soil moisture reading? (pre/post, blank to skip)", ""
    ).strip().lower()
    if answer in ("pre", "post"):
        readings.append(prompt_reading(prompt_phase=False, default_phase=answer))
    return {
        "start": start,
        "end": end,
        "minutes": minutes,
        "rain_mm": rain,
        "intensity": intensity,
        "readings": readings,
        "notes": notes,
    }


def render_scale(stream) -> None:
    stream.write("Petrichor intensity scale:\n")
    for level, name, descriptor in INTENSITY_SCALE:
        stream.write(f"  {level} — {name}: {descriptor}\n")


# --------------------------------------------------------------------------
# Reading assembly from flags
# --------------------------------------------------------------------------
def reading_from_flags(args: argparse.Namespace) -> dict | None:
    if args.pre is not None and args.post is not None:
        raise CliError("use --pre or --post, not both")
    if args.percent is not None and args.relative is not None:
        raise CliError("use --percent or --relative, not both")

    if args.depth is not None:
        check_depth(args.depth)

    phase = None
    if args.pre is not None:
        phase = "pre"
    elif args.post is not None:
        phase = "post"

    unit = "relative" if args.relative is not None else "percent"
    if args.relative is not None:
        value: float | None = check_relative(args.relative)
    elif args.percent is not None:
        value = check_percent(args.percent)
    elif args.pre is not None:
        value = check_percent(args.pre)
    elif args.post is not None:
        value = check_percent(args.post)
    else:
        value = None

    has_extra = any(
        item is not None
        for item in (args.percent, args.relative, args.depth, args.obs)
    )
    if phase is None:
        if has_extra:
            raise CliError("provide --pre or --post to record a moisture reading")
        return None

    return {
        "ts_iso": local_now().isoformat(),
        "phase": phase,
        "value": round(float(value), 3),
        "unit": unit,
        "depth_cm": round(float(args.depth if args.depth is not None else 0.0), 3),
        "obs": args.obs or "",
    }


# --------------------------------------------------------------------------
# Event construction and rendering
# --------------------------------------------------------------------------
def build_event(
    event_id: int,
    start: datetime,
    end: datetime,
    minutes: int,
    rain_mm: float,
    intensity: int,
    suggested: int,
    notes: str,
    readings: list[dict],
    now_iso: str,
) -> dict:
    return {
        "id": event_id,
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "minutes": minutes,
        "rain_mm": round(float(rain_mm), 3),
        "intensity": intensity,
        "intensity_suggested": suggested,
        "intensity_overridden": intensity != suggested,
        "notes": notes,
        "readings": readings,
        "created_iso": now_iso,
        "updated_iso": now_iso,
    }


def paired_moisture(readings: list[dict]) -> tuple[dict | None, dict | None]:
    """Earliest pre reading and latest post reading, by ts_iso then insertion."""
    pres = sorted(
        (str(reading["ts_iso"]), index, reading)
        for index, reading in enumerate(readings)
        if reading.get("phase") == "pre"
    )
    posts = sorted(
        (str(reading["ts_iso"]), index, reading)
        for index, reading in enumerate(readings)
        if reading.get("phase") == "post"
    )
    return (pres[0][2] if pres else None, posts[-1][2] if posts else None)


def moisture_delta(readings: list[dict]) -> tuple[float | None, str | None]:
    """(delta, unit), or (None, human status) when it cannot be computed."""
    pre, post = paired_moisture(readings)
    if pre is None or post is None:
        return None, "no complete pre/post pair"
    if pre.get("unit") != post.get("unit"):
        return None, "mixed units — delta not computed"
    return float(post["value"]) - float(pre["value"]), str(pre.get("unit"))


def render_event_detail(event: dict) -> str:
    lines = [f"Event {event['id']}"]
    lines.append(f"  start       {event['start_iso']}")
    lines.append(f"  end         {event['end_iso'] or '—'}")
    lines.append(f"  minutes     {event['minutes']}")
    lines.append(f"  rain        {format_mm(float(event['rain_mm']))}")
    chosen = int(event["intensity"])
    suggested = int(event.get("intensity_suggested", chosen))
    override = "overridden" if event.get("intensity_overridden") else "as suggested"
    lines.append(
        f"  intensity   {intensity_label(chosen)} "
        f"(suggested {intensity_label(suggested)}; {override})"
    )
    lines.append(f"  notes       {event['notes'] or '—'}")
    lines.append(f"  created     {event['created_iso']}")
    lines.append(f"  updated     {event.get('updated_iso', event['created_iso'])}")

    readings = event.get("readings", []) or []
    if readings:
        lines.append("  readings")
        for reading in readings:
            observations = reading.get("obs") or "—"
            lines.append(
                f"    {reading['ts_iso']}  {reading['phase']:<4} "
                f"{float(reading['value']):g} {reading['unit']}  "
                f"depth {format_depth(float(reading.get('depth_cm', 0.0)))}  "
                f"{observations}"
            )
    else:
        lines.append("  readings    none yet")
        lines.append(f"              add one with 'petrichorlog log note {event['id']}'")

    delta, status = moisture_delta(readings)
    if delta is None:
        lines.append(f"  moisture    {status}")
    else:
        lines.append(f"  moisture    {format_delta(delta, status or '')}")
    return "\n".join(lines)


def render_event_row(event: dict) -> str:
    start = str(event["start_iso"])
    day = start[:10]
    clock = start[11:16] if len(start) >= 16 else "--:--"
    return (
        f"{int(event['id']):>3}  {day}  {clock}  "
        f"{float(event['rain_mm']):>6.1f} mm  "
        f"{intensity_label(int(event['intensity'])):<18}  "
        f"{int(event['minutes']):>4} min"
    )


# --------------------------------------------------------------------------
# Season visualiser
# --------------------------------------------------------------------------
def bar_width_for(columns: int) -> int:
    return min(MAX_BAR_WIDTH, max(MIN_BAR_WIDTH, columns - SEASON_FIXED_COLUMNS))


def event_year_month(event: dict) -> tuple[int, int] | None:
    start = str(event.get("start_iso", ""))
    if len(start) < 7:
        return None
    try:
        return int(start[:4]), int(start[5:7])
    except ValueError:
        return None


def season_months(events: list[dict], year: int) -> list[dict]:
    months = [
        {
            "month": f"{year}-{month:02d}",
            "events": 0,
            "rain_mm": 0.0,
            "intensity_total": 0.0,
            "deltas": [],
        }
        for month in range(1, 13)
    ]
    for event in events:
        parts = event_year_month(event)
        if parts is None or parts[0] != year or not 1 <= parts[1] <= 12:
            continue
        bucket = months[parts[1] - 1]
        bucket["events"] += 1
        bucket["rain_mm"] += float(event["rain_mm"])
        bucket["intensity_total"] += int(event["intensity"])
        delta, unit = moisture_delta(event.get("readings", []) or [])
        if delta is not None:
            bucket["deltas"].append((delta, unit))
    return months


def summarise_delta(deltas: list[tuple[float, str]]) -> tuple[float | None, str | None]:
    if not deltas:
        return None, None
    units = {unit for _, unit in deltas}
    if len(units) > 1:
        return None, "mixed"
    return sum(delta for delta, _ in deltas) / len(deltas), units.pop()


def format_month_delta(average: float | None, unit: str | None) -> str:
    if unit is None:
        return "n/a"
    if unit == "mixed":
        return "mixed"
    if unit == "percent":
        return f"{average:+.1f}%"
    return f"{average:+.2f}rel"


def render_season(events: list[dict], year: int) -> str:
    months = season_months(events, year)
    count = sum(bucket["events"] for bucket in months)
    if count == 0:
        return f"No events for {year} yet — add one with 'petrichorlog log add'."

    total_rain = sum(bucket["rain_mm"] for bucket in months)
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    width = bar_width_for(columns)
    peak = max(bucket["rain_mm"] for bucket in months) or 1.0
    rule = THEME["rule"] * min(max(columns, 40), 78)

    lines = [
        f"Season {year} — {count} event{'s' if count != 1 else ''}, "
        f"{total_rain:.1f} mm total",
        rule,
        f"{'month':<7} {'rain mm':>7}  {'bar':<{width}}  "
        f"{'ev':>3}  {'int':>4}  {'moisture':>8}",
        rule,
    ]
    for bucket in months:
        rain = bucket["rain_mm"]
        if bucket["events"] and rain > 0:
            bar = THEME["bar"] * max(1, round(rain / peak * width))
            intensity = f"{bucket['intensity_total'] / bucket['events']:.1f}"
        else:
            bar = ""
            intensity = "—"
        average, unit = summarise_delta(bucket["deltas"])
        lines.append(
            f"{bucket['month']:<7} {rain:>7.1f}  {bar:<{width}}  "
            f"{bucket['events']:>3}  {intensity:>4}  "
            f"{format_month_delta(average, unit):>8}"
        )
    lines.append(rule)
    lines.append("moisture = mean paired change (percent unless marked rel; n/a = no pair)")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------
def plan_add_from_flags(args: argparse.Namespace, now: datetime) -> dict:
    """Validate and resolve flag-mode input without touching the store."""
    if args.minutes is not None and args.end is not None:
        raise CliError("provide either --minutes or --end, not both")

    rain_mm = check_rain(args.rain if args.rain is not None else 0.0)
    intensity = check_intensity(args.intensity) if args.intensity is not None else None
    reading = reading_from_flags(args)

    start = parse_start(args.start or "", now)
    if args.minutes is not None:
        minutes = check_minutes(int(args.minutes))
        end = start + timedelta(minutes=minutes)
    elif args.end is not None:
        end = parse_end(args.end, start)
        minutes = minutes_between(start, end)
    else:
        raise CliError("provide --minutes or --end")

    return {
        "start": start,
        "end": end,
        "minutes": minutes,
        "rain_mm": rain_mm,
        "intensity": intensity,
        "readings": [reading] if reading is not None else [],
        "notes": args.notes or "",
    }


def command_add(args: argparse.Namespace) -> int:
    now = local_now()
    flag_mode = any(
        item is not None
        for item in (
            args.start,
            args.minutes,
            args.end,
            args.rain,
            args.intensity,
            args.notes,
            args.pre,
            args.post,
            args.percent,
            args.relative,
            args.depth,
            args.obs,
        )
    )

    # Validate the flags before the store is opened, so a rejected command
    # never creates or rewrites a file.
    plan = plan_add_from_flags(args, now) if flag_mode else None
    store, path = open_store(args)
    if plan is None:
        plan = prompt_event_fields()

    suggested = suggest_intensity(plan["rain_mm"], plan["minutes"])
    intensity = plan["intensity"] if plan["intensity"] is not None else suggested
    event = build_event(
        event_id=int(store["next_id"]),
        start=plan["start"],
        end=plan["end"],
        minutes=plan["minutes"],
        rain_mm=plan["rain_mm"],
        intensity=intensity,
        suggested=suggested,
        notes=plan["notes"],
        readings=plan["readings"],
        now_iso=now.isoformat(),
    )
    store["events"].append(event)
    store["next_id"] = int(event["id"]) + 1
    save_store(path, store)

    print(f"Logged rain event {event['id']}")
    print(f"  start      {event['start_iso']}")
    print(f"  end        {event['end_iso']}")
    print(f"  duration   {event['minutes']} min")
    print(f"  rain       {format_mm(float(event['rain_mm']))}")
    print(f"  intensity  {intensity_label(int(event['intensity']))}")
    print(f"  suggested  {intensity_line(suggested)}")
    if event["readings"]:
        first = event["readings"][0]
        print(
            f"  reading    {first['phase']} {float(first['value']):g} {first['unit']} "
            f"at depth {format_depth(float(first['depth_cm']))}"
        )
    print(f"  view it with 'petrichorlog log show {event['id']}'")
    return EXIT_OK



def command_list(args: argparse.Namespace) -> int:
    store, _path = open_store(args)
    events = sorted(
        store["events"],
        key=lambda event: (str(event["start_iso"]), int(event["id"])),
        reverse=True,
    )
    if args.json:
        print(json.dumps({"count": len(events), "events": events}, indent=2))
        return EXIT_OK
    if not events:
        print("No events yet — add one with 'petrichorlog log add'.")
        return EXIT_OK

    print(
        f"{'id':>3}  {'date':<10}  {'time':<5}  {'rain':>9}  "
        f"{'intensity':<18}  {'len':>7}"
    )
    print(THEME["rule"] * 66)
    for event in events:
        print(render_event_row(event))
    print(THEME["rule"] * 66)
    print(f"{len(events)} event{'s' if len(events) != 1 else ''} logged, newest first.")
    print(f"Total logged rain: {sum(float(e['rain_mm']) for e in events):.1f} mm")
    return EXIT_OK


def command_show(args: argparse.Namespace) -> int:
    store, _path = open_store(args, create=False)
    event = find_event(store, args.id)
    if args.json:
        print(json.dumps(event, indent=2))
        return EXIT_OK
    print(render_event_detail(event))
    return EXIT_OK


def command_note(args: argparse.Namespace) -> int:
    flag_mode = any(
        item is not None
        for item in (
            args.pre,
            args.post,
            args.percent,
            args.relative,
            args.depth,
            args.obs,
        )
    )
    reading = reading_from_flags(args) if flag_mode else None
    if flag_mode and reading is None:
        raise CliError("provide --pre or --post to record a moisture reading")

    store, path = open_store(args)
    event = find_event(store, args.id)
    if reading is None:
        reading = prompt_reading()

    event["readings"].append(reading)
    event["updated_iso"] = local_now().isoformat()
    save_store(path, store)

    print(
        f"Added {reading['phase']} reading to event {event['id']} "
        f"({float(reading['value']):g} {reading['unit']}, "
        f"depth {format_depth(float(reading['depth_cm']))})"
    )
    delta, status = moisture_delta(event["readings"])
    if delta is None:
        print(f"  moisture   {status}")
    else:
        print(f"  moisture   {format_delta(delta, status or '')}")
    return EXIT_OK



def command_season(args: argparse.Namespace) -> int:
    store, _path = open_store(args)
    year = args.year if args.year is not None else date.today().year
    if args.json:
        payload = []
        for bucket in season_months(store["events"], year):
            average, unit = summarise_delta(bucket["deltas"])
            payload.append(
                {
                    "month": bucket["month"],
                    "events": bucket["events"],
                    "rain_mm": round(bucket["rain_mm"], 3),
                    "avg_intensity": (
                        round(bucket["intensity_total"] / bucket["events"], 2)
                        if bucket["events"]
                        else None
                    ),
                    "avg_delta": None if average is None else round(average, 3),
                    "delta_unit": unit,
                }
            )
        print(json.dumps({"year": year, "months": payload}, indent=2))
        return EXIT_OK
    print(render_season(store["events"], year))
    return EXIT_OK


# --------------------------------------------------------------------------
# Argparse wiring
# --------------------------------------------------------------------------
EXAMPLES = """\
examples:
  petrichorlog log add --rain 4 --minutes 30 --intensity 3
  petrichorlog log add --rain 12 --minutes 45 --notes "warm ground, smell arrived late"
  petrichorlog log add --start 09:00 --minutes 30 --pre 31 --depth 10 --obs "dry crust"
  petrichorlog log note 1 --post 45 --depth 10 --obs "soaked through"
  petrichorlog log list
  petrichorlog log show 1
  petrichorlog log season --year 2025
  cat passphrase.txt | petrichorlog --passphrase-stdin log list

notes:
  --pre/--post choose the reading phase, --percent/--relative choose its unit.
  In flag mode nothing ever prompts: a bad value prints one line and exits non-zero.
  With no reading flags, 'log add' and 'log note <id>' open the interactive wizard.
"""

SCALE_HELP = "\n".join(
    f"  {level} — {name}: {descriptor}" for level, name, descriptor in INTENSITY_SCALE
)


def add_common_options(parser: argparse.ArgumentParser, leaf: bool = False) -> None:
    parser.add_argument(
        "--store",
        metavar="PATH",
        default=argparse.SUPPRESS if leaf else None,
        help=f"store file to use (default: ${STORE_ENV} or ./{DEFAULT_STORE})",
    )
    parser.add_argument(
        "--passphrase-stdin",
        action="store_true",
        default=argparse.SUPPRESS if leaf else False,
        help="read the store passphrase from a single stdin line; never prompts",
    )


def add_reading_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pre", type=float, metavar="VALUE", help="pre-rain soil moisture value"
    )
    parser.add_argument(
        "--post", type=float, metavar="VALUE", help="post-rain soil moisture value"
    )
    parser.add_argument(
        "--percent", type=float, metavar="VALUE", help="moisture value as percent (0-100)"
    )
    parser.add_argument(
        "--relative", type=float, metavar="VALUE", help="moisture value in relative units (>0)"
    )
    parser.add_argument(
        "--depth", type=float, metavar="CM", help="reading depth in cm (default 0)"
    )
    parser.add_argument("--obs", metavar="TEXT", help="observation for the reading")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "PetrichorLog — log rain events with soil moisture, and watch the "
            "seasonal petrichor pattern."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    add_common_options(parser)

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    log = subparsers.add_parser(
        "log",
        help="record and review logged rain events",
        description="Record rain events, review them, or visualise the season.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    log.set_defaults(log_parser=log)
    log_sub = log.add_subparsers(dest="log_command", metavar="SUBCOMMAND")

    add = log_sub.add_parser(
        "add",
        help="log a new rain event (interactive wizard, or flags)",
        description=(
            "Log a new rain event. With no flags this opens the interactive "
            "wizard, which lists the full intensity scale."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common_options(add, leaf=True)
    add.add_argument("--start", metavar="TIME", help="start HH:MM or ISO-8601 (default: now)")
    add.add_argument("--minutes", type=int, metavar="N", help="duration in minutes")
    add.add_argument(
        "--end", metavar="TIME", help="end HH:MM (same day as start) or ISO-8601"
    )
    add.add_argument("--rain", type=float, metavar="MM", help="rainfall in millimetres (default 0)")
    add.add_argument(
        "--intensity", type=float, metavar="0-5", help="petrichor intensity (default: suggested)"
    )
    add.add_argument("--notes", metavar="TEXT", help="free-form notes")
    add_reading_options(add)
    add.set_defaults(handler=command_add)

    listing = log_sub.add_parser(
        "list",
        help="list logged events, newest first",
        description="List logged events newest first, one summary line each.",
    )
    add_common_options(listing, leaf=True)
    listing.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    listing.set_defaults(handler=command_list)

    show = log_sub.add_parser(
        "show",
        help="show one event in full detail",
        description=(
            "Show one event: timing, rain, chosen vs suggested intensity, notes, "
            "all moisture readings and the paired delta."
        ),
    )
    add_common_options(show, leaf=True)
    show.add_argument("id", type=int, help="event id")
    show.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    show.set_defaults(handler=command_show)

    note = log_sub.add_parser(
        "note",
        help="append a soil moisture reading to an event",
        description=(
            "Append a pre or post soil moisture reading to an existing event. "
            "With no reading flags this prompts for phase, value, unit, depth "
            "and observation."
        ),
    )
    add_common_options(note, leaf=True)
    note.add_argument("id", type=int, help="event id")
    add_reading_options(note)
    note.set_defaults(handler=command_note)

    season = log_sub.add_parser(
        "season",
        help="monthly seasonal pattern for one year",
        description=(
            "Monthly bars of total rain, average intensity, event count and "
            "paired moisture delta for one year."
        ),
    )
    add_common_options(season, leaf=True)
    season.add_argument(
        "--year", type=int, metavar="YYYY", help="year to summarise (default: this year)"
    )
    season.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    season.set_defaults(handler=command_season)

    return parser


def print_banner() -> None:
    print(f"PetrichorLog {__version__} — local rain-event log with soil moisture notes.")
    print()
    print("Petrichor intensity scale:")
    print(SCALE_HELP)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        print_banner()
        print()
        parser.print_help()
        return EXIT_OK

    if args.command == "log" and getattr(args, "log_command", None) is None:
        args.log_parser.print_help()
        return EXIT_OK

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE

    try:
        return handler(args)
    except CliError as exc:
        sys.stderr.write(f"{PROG}: {exc}\n")
        return exc.code
    except KeyboardInterrupt:
        sys.stderr.write(f"{PROG}: interrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
