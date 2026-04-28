from __future__ import annotations

import datetime as dt
import multiprocessing

import httpx
import pytest

from sentinel.scheduler import (
    DailyRunCounter,
    SchedulerLock,
    parse_interval,
    post_cycle_result,
    run_scheduled_ticks,
)


def _try_lock(lock_path, queue) -> None:  # noqa: ANN001
    try:
        with SchedulerLock(lock_path):
            queue.put("")
    except RuntimeError as exc:
        queue.put(str(exc))
        raise


def test_schedule_parses_human_intervals() -> None:
    assert parse_interval("every 4h") == dt.timedelta(hours=4)
    assert parse_interval("every 30m") == dt.timedelta(minutes=30)
    assert parse_interval("every 1d") == dt.timedelta(days=1)


def test_schedule_parses_cron_expressions() -> None:
    interval = parse_interval("0 */4 * * *")

    assert dt.timedelta(0) < interval <= dt.timedelta(hours=4)


def test_lockfile_prevents_concurrent_runs(tmp_path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    queue = multiprocessing.Queue()

    with SchedulerLock(lock_path):
        process = multiprocessing.Process(target=_try_lock, args=(lock_path, queue))
        process.start()
        process.join(timeout=5)

    assert process.exitcode not in (0, None)
    assert "already running" in queue.get(timeout=1)


def test_lockfile_releases_on_exit(tmp_path) -> None:
    lock_path = tmp_path / "scheduler.lock"

    with SchedulerLock(lock_path):
        pass
    assert not lock_path.exists()
    with SchedulerLock(lock_path):
        pass


def test_max_runs_per_day_respected() -> None:
    counter = DailyRunCounter(max_runs=3)

    assert counter.check_and_increment() is True
    assert counter.check_and_increment() is True
    assert counter.check_and_increment() is True
    assert counter.check_and_increment() is False


@pytest.mark.asyncio
async def test_tick_generates_unique_cycle_id() -> None:
    cycle_ids: list[str] = []

    async def fake_cycle(cycle_id: str) -> dict:
        cycle_ids.append(cycle_id)
        return {"cycle_id": cycle_id, "status": "completed"}

    async def fake_sleep(_seconds: float) -> None:
        return None

    executed = await run_scheduled_ticks(
        interval_spec="every 30m",
        max_runs_per_day=0,
        delivery_webhook="",
        run_cycle=fake_cycle,
        sleep=fake_sleep,
        stop_after_ticks=3,
    )

    assert executed == 3
    assert len(cycle_ids) == 3
    assert len(set(cycle_ids)) == 3


def test_webhook_posted_after_cycle(monkeypatch) -> None:
    calls = []

    def fake_post(url, *, json, timeout):  # noqa: ANN001
        calls.append((url, json, timeout))

    monkeypatch.setattr("sentinel.scheduler.httpx.post", fake_post)

    post_cycle_result("https://example.com/hook", {"status": "completed"})

    assert calls == [
        ("https://example.com/hook", {"status": "completed"}, 10),
    ]


def test_webhook_slack_wrapped(monkeypatch) -> None:
    calls = []

    def fake_post(url, *, json, timeout):  # noqa: ANN001
        calls.append((url, json, timeout))

    monkeypatch.setattr("sentinel.scheduler.httpx.post", fake_post)

    post_cycle_result("https://hooks.slack.com/services/test", {"status": "completed"})

    assert calls[0][0] == "https://hooks.slack.com/services/test"
    assert "text" in calls[0][1]
    assert calls[0][2] == 10


def test_webhook_failure_does_not_raise(monkeypatch) -> None:
    def fake_post(url, *, json, timeout):  # noqa: ANN001, ARG001
        raise httpx.ConnectError("no route")

    monkeypatch.setattr("sentinel.scheduler.httpx.post", fake_post)

    post_cycle_result("https://example.com/hook", {"status": "completed"})


@pytest.mark.asyncio
async def test_webhook_failure_logged_but_doesnt_fail_cycle(monkeypatch) -> None:
    cycle_ids: list[str] = []

    def fake_post(url, *, json, timeout):  # noqa: ANN001, ARG001
        raise httpx.ConnectError("no route")

    async def fake_cycle(cycle_id: str) -> dict:
        cycle_ids.append(cycle_id)
        return {"cycle_id": cycle_id, "status": "completed"}

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("sentinel.scheduler.httpx.post", fake_post)

    executed = await run_scheduled_ticks(
        interval_spec="every 30m",
        max_runs_per_day=0,
        delivery_webhook="https://example.com/hook",
        run_cycle=fake_cycle,
        sleep=fake_sleep,
        stop_after_ticks=2,
    )

    assert executed == 2
    assert len(cycle_ids) == 2


@pytest.mark.asyncio
async def test_scheduled_ticks_respect_max_runs_per_day() -> None:
    cycle_ids: list[str] = []

    async def fake_cycle(cycle_id: str) -> dict:
        cycle_ids.append(cycle_id)
        return {"cycle_id": cycle_id, "status": "completed"}

    async def fake_sleep(_seconds: float) -> None:
        return None

    executed = await run_scheduled_ticks(
        interval_spec="every 30m",
        max_runs_per_day=2,
        delivery_webhook="",
        run_cycle=fake_cycle,
        sleep=fake_sleep,
        stop_after_ticks=4,
    )

    assert executed == 2
    assert len(cycle_ids) == 2


@pytest.mark.asyncio
async def test_sigint_cancels_sleep() -> None:
    cycle_ids: list[str] = []

    async def fake_cycle(cycle_id: str) -> dict:
        cycle_ids.append(cycle_id)
        return {"cycle_id": cycle_id, "status": "completed"}

    async def interrupted_sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    executed = await run_scheduled_ticks(
        interval_spec="every 30m",
        max_runs_per_day=0,
        delivery_webhook="",
        run_cycle=fake_cycle,
        sleep=interrupted_sleep,
    )

    assert executed == 1
    assert len(cycle_ids) == 1


def test_parse_interval_invalid() -> None:
    with pytest.raises(ValueError):
        parse_interval("every banana")
