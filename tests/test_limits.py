from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from poc.store import LimitError, Store


SEOUL = ZoneInfo("Asia/Seoul")


def video(n: int) -> str:
    return f"v{n:010d}"


def submitted(store: Store, n: int) -> int:
    attempt = store.reserve(video(n), "youtube1", "test")
    store.stage(attempt, "opening")
    store.stage(attempt, "typing")
    store.begin_send(attempt)
    store.confirm(attempt)
    return attempt


def test_duplicate_video_across_url_and_profile(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    submitted(store, 1)
    with pytest.raises(LimitError):
        store.reserve(f"https://youtu.be/{video(1)}?t=10", "youtube2", "other")
    assert store.snapshot()["count"] == 1


def test_daily_limit_allows_tenth_and_blocks_eleventh(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    for i in range(9):
        submitted(store, i)
    store.reserve(video(9), "youtube1", "test")
    with pytest.raises(LimitError):
        store.reserve(video(10), "youtube1", "test")
    assert store.snapshot()["count"] == 10


def test_failed_before_send_can_retry_but_uncertain_cannot(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    first = store.reserve(video(1), "youtube1", "test")
    store.stage(first, "opening")
    assert store.fail(first, "opening failed") == "failed"
    second = store.reserve(video(1), "youtube1", "test")
    store.stage(second, "opening")
    store.stage(second, "typing")
    store.begin_send(second)
    assert store.fail(second, "connection lost") == "uncertain"
    with pytest.raises(LimitError):
        store.reserve(video(1), "youtube2", "test")
    assert store.snapshot()["count"] == 1


def test_simultaneous_duplicate_reservations_have_one_winner(tmp_path):
    store = Store(tmp_path / "test.sqlite3")

    def reserve(_):
        try:
            return store.reserve(video(1), "youtube1", "test")
        except LimitError:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(reserve, range(4)))
    assert sum(result is not None for result in results) == 1
    assert store.snapshot()["count"] == 1


def test_midnight_rechecks_limit_before_sending(tmp_path):
    current = [datetime(2026, 11, 23, 23, 59, tzinfo=SEOUL)]
    store = Store(tmp_path / "test.sqlite3", now=lambda: current[0])
    waiting = store.reserve(video(99), "youtube1", "test")
    store.stage(waiting, "opening")
    store.stage(waiting, "typing")
    current[0] += timedelta(minutes=2)
    for i in range(10):
        submitted(store, i)
    with pytest.raises(LimitError):
        store.begin_send(waiting)
    assert store.fail(waiting, "new day full") == "failed"
    assert store.snapshot()["count"] == 10


def test_records_persist_after_reopening(tmp_path):
    path = tmp_path / "test.sqlite3"
    store = Store(path)
    attempt = submitted(store, 1)
    reopened = Store(path)
    assert reopened.get(attempt)["status"] == "confirmed"
    assert reopened.snapshot()["count"] == 1


def test_deleted_comment_remains_counted_and_video_stays_blocked(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    attempt = submitted(store, 1)
    store.record_deletion(attempt, True)
    assert store.get(attempt)["deletion_status"] == "deleted"
    assert store.snapshot()["count"] == 1
    with pytest.raises(LimitError):
        store.reserve(video(1), "youtube2", "test")


def test_round_robin_is_persistent_and_keeps_the_same_profile_on_retry(tmp_path):
    path = tmp_path / "round-robin.sqlite3"
    store = Store(path)
    first = store.reserve(video(1), "auto", "test")
    assert store.get(first)["profile"] == "youtube1"
    store.fail(first, "opening failed")
    retry = store.reserve(video(1), "auto", "test")
    assert store.get(retry)["profile"] == "youtube1"
    second = Store(path).reserve(video(2), "auto", "test")
    third = Store(path).reserve(video(3), "auto", "test")
    fourth = Store(path).reserve(video(4), "auto", "test")
    assert [store.get(i)["profile"] for i in (second, third, fourth)] == ["youtube2", "youtube3", "youtube1"]


def test_manual_profile_does_not_advance_automatic_rotation(tmp_path):
    store = Store(tmp_path / "profiles.sqlite3")
    manual = store.reserve(video(1), "youtube3", "test")
    automatic = store.reserve(video(2), "auto", "test")
    assert store.get(manual)["profile"] == "youtube3"
    assert store.get(automatic)["profile"] == "youtube1"


def test_previously_draft_only_videos_can_be_posted_after_restart(tmp_path):
    path = tmp_path / "legacy-rising.sqlite3"
    old_store = Store(path)
    with old_store._connect() as db:
        db.execute("CREATE TABLE draft_only_videos (video_id TEXT PRIMARY KEY, source TEXT NOT NULL, identified_at TEXT NOT NULL)")
        db.executemany("INSERT INTO draft_only_videos VALUES (?, ?, ?)",
                       [(video(n), "rising", "2026-11-26") for n in (1, 2)])
    store = Store(path)
    first = store.reserve(video(1), "youtube1", "test")
    store.stage(first, "opening")
    store.stage(first, "typing")
    store.begin_send(first)
    second = store.reserve(video(2), "youtube3", "test")
    assert store.get(first)["status"] == "submitting"
    assert store.get(second)["profile"] == "youtube3"
    with pytest.raises(LimitError):
        store.reserve(video(1), "youtube2", "duplicate")


def test_parallel_round_robin_assignments_do_not_share_a_cursor_step(tmp_path):
    store = Store(tmp_path / "parallel-profiles.sqlite3")
    with ThreadPoolExecutor(max_workers=6) as pool:
        attempts = list(pool.map(lambda i: store.reserve(video(i), "auto", "test"), range(6)))
    profiles = [store.get(attempt)["profile"] for attempt in attempts]
    assert sorted(profiles) == ["youtube1", "youtube1", "youtube2", "youtube2", "youtube3", "youtube3"]
