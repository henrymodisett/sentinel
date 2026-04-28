"""Scheduling helpers for `sentinel work --schedule`."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Self

import httpx
from rich.console import Console  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

console = Console()

CycleRunner = Callable[[str], Awaitable[dict[str, Any] | None]]
SleepFunc = Callable[[float], Awaitable[None]]


def parse_interval(spec: str) -> dt.timedelta:
    """Parse human interval or cron expression to timedelta for sleep duration.

    Human: "every 4h", "every 30m", "every 1d"
    Cron: "0 */4 * * *" returns time until next fire from now.
    """
    raw = spec.strip()
    human = raw.lower()
    if human.startswith("every "):
        human = human.removeprefix("every ").strip()
    if match := re.fullmatch(r"(\d+)(m|h|d)", human):
        value = int(match.group(1))
        unit = match.group(2)
        if unit == "m":
            return dt.timedelta(minutes=value)
        if unit == "h":
            return dt.timedelta(hours=value)
        return dt.timedelta(days=value)

    fields = raw.split()
    if len(fields) == 5:
        return _parse_cron_interval(raw)

    raise ValueError(
        f"Unrecognized schedule interval {spec!r}; use 'every 30m', "
        "'every 4h', 'every 1d', or a 5-field cron expression.",
    )


def new_cycle_id(now: dt.datetime | None = None) -> str:
    """Return a run-journal-safe cycle id for one scheduled tick."""
    current = now or dt.datetime.now()
    return f"{current.strftime('%Y-%m-%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


async def run_scheduled_ticks(
    *,
    interval_spec: str,
    max_runs_per_day: int,
    delivery_webhook: str,
    run_cycle: CycleRunner,
    sleep: SleepFunc,
    stop_after_ticks: int | None = None,
) -> int:
    """Run scheduled ticks.

    This contains the scheduler invariants that are easy to regress:
    every executed tick gets a fresh cycle id, the daily cap turns ticks
    into no-ops, webhook delivery is best-effort, and sleep interruption
    exits cleanly.
    """
    run_counter = DailyRunCounter(max_runs_per_day)
    executed = 0
    ticks = 0

    while stop_after_ticks is None or ticks < stop_after_ticks:
        ticks += 1
        if run_counter.check_and_increment():
            payload = await run_cycle(new_cycle_id())
            executed += 1
            if payload is not None:
                post_cycle_result(delivery_webhook, payload)

        try:
            await sleep(parse_interval(interval_spec).total_seconds())
        except KeyboardInterrupt:
            break

    return executed


def _parse_cron_interval(spec: str) -> dt.timedelta:
    now = dt.datetime.now()
    try:
        import croniter  # type: ignore[import-untyped,import-not-found]

        next_fire = croniter.croniter(spec, now).get_next(dt.datetime)
        return next_fire - now
    except ImportError:
        return _simple_cron_interval(spec, now)
    except Exception as exc:
        raise ValueError(f"Invalid cron schedule {spec!r}: {exc}") from exc


def _simple_cron_interval(spec: str, now: dt.datetime) -> dt.timedelta:
    fields = spec.split()
    minute, hour, day, month, weekday = fields
    if day != "*" or month != "*" or weekday != "*":
        raise ValueError(
            f"Unsupported cron schedule {spec!r} without croniter; "
            "only simple minute/hour */N patterns are supported.",
        )

    for offset_minutes in range(1, 366 * 24 * 60):
        candidate = (now + dt.timedelta(minutes=offset_minutes)).replace(
            second=0,
            microsecond=0,
        )
        if _cron_field_matches(minute, candidate.minute, 0, 59) and _cron_field_matches(
            hour,
            candidate.hour,
            0,
            23,
        ):
            return candidate - now

    raise ValueError(f"Could not calculate next fire for cron schedule {spec!r}")


def _cron_field_matches(field: str, value: int, minimum: int, maximum: int) -> bool:
    if field == "*":
        return True
    if field.isdigit():
        return value == int(field)
    if match := re.fullmatch(r"\*/(\d+)", field):
        step = int(match.group(1))
        if step <= 0:
            raise ValueError(f"Invalid cron step {field!r}")
        return (value - minimum) % step == 0 and minimum <= value <= maximum
    raise ValueError(f"Unsupported cron field {field!r}")


class SchedulerLock:
    """Advisory lock via fcntl.flock; survives process kills, no stale lock."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            pid = _read_lock_pid(self.lock_path)
            os.close(fd)
            raise RuntimeError(
                f"scheduler already running (pid {pid}); remove {self.lock_path} if stale",
            ) from exc

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        if self._fd is None:
            return
        with suppress(FileNotFoundError):
            self.lock_path.unlink()
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


def _read_lock_pid(lock_path: Path) -> str:
    try:
        pid = lock_path.read_text().strip()
    except OSError:
        return "unknown"
    return pid or "unknown"


def post_cycle_result(webhook_url: str, payload: dict) -> None:
    """POST cycle result JSON to webhook. Logs on failure, never raises."""
    if not webhook_url:
        return

    body = payload
    if "hooks.slack.com" in webhook_url:
        body = {"text": json.dumps(payload)}

    try:
        response = httpx.post(webhook_url, json=body, timeout=10)
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - webhook delivery is best-effort
        console.print(f"  [yellow]Webhook delivery failed: {exc}[/yellow]")


class DailyRunCounter:
    """Tracks runs per calendar day; resets automatically at midnight."""

    def __init__(self, max_runs: int):
        self.max_runs = max_runs
        self._date = dt.date.today()
        self._runs = 0

    def check_and_increment(self) -> bool:
        """Returns True if run is allowed, False if daily cap reached."""
        today = dt.date.today()
        if today != self._date:
            self._date = today
            self._runs = 0

        if self.max_runs <= 0:
            self._runs += 1
            return True
        if self._runs >= self.max_runs:
            return False

        self._runs += 1
        return True
