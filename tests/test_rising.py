import pytest

import poc.rising as rising
from poc.store import Store


def test_view_observations_require_interval_and_survive_restart(tmp_path):
    path = tmp_path / "views.sqlite3"
    first = Store(path)
    assert first.observe_views("abcdefghij1", 1000, 1000) is None
    assert first.observe_views("abcdefghij1", 1002, 1059) is None
    measured = Store(path).observe_views("abcdefghij1", 1003, 1060)
    assert measured["elapsed_seconds"] == 60
    assert measured["views_added"] == 3 and measured["views_per_hour"] == 180
    assert Store(path).observe_views("abcdefghij1", 995, 1120) is None
    assert Store(path).observe_views("abcdefghij2", 1000, 1000) is None
    assert Store(path).observe_views("abcdefghij2", 1500, 4601) is None
    with pytest.raises(ValueError):
        first.observe_views("abcdefghij1", -1, 1061)


def test_rising_search_uses_real_observation_delta_and_allows_posting(tmp_path, monkeypatch):
    rows = [
        {"id": "abcdefghij1", "views": 1000},
        {"id": "abcdefghij2", "views": 4900},
        {"id": "abcdefghij3", "views": 4999},
        {"id": "abcdefghij4", "views": 5100},
    ]
    monkeypatch.setattr(rising, "search_flat_videos", lambda *args, **kwargs: rows)
    values = {"abcdefghij1": [1000, 1003], "abcdefghij2": [4900, 4901],
              "abcdefghij3": [4999, 5001], "abcdefghij4": [5100]}
    def get_video(vid):
        return {"id": vid, "views": values[vid].pop(0), "published": None, "published_timestamp": None}
    monkeypatch.setattr(rising, "get_video", get_video)
    now = [1000]
    events = []
    store = Store(tmp_path / "views.sqlite3")
    result = rising.find_rising(store, lambda **x: events.append(x), clock=lambda: now[0],
                                wait=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert [video["id"] for video in result["videos"]] == ["abcdefghij1"]
    assert result["videos"][0]["measurement"]["views_per_hour"] == 180
    assert "draft_only" not in result["videos"][0]
    attempt = store.reserve(result["videos"][0]["id"], "youtube1", "test")
    store.stage(attempt, "opening")
    store.stage(attempt, "typing")
    store.begin_send(attempt)
    assert store.get(attempt)["status"] == "submitting"
    assert any(event["stage"] == "observing" for event in events)
    assert result["searched"] == 4 and result["measured"] == 3


def test_rising_search_reuses_an_earlier_measurement_without_waiting(tmp_path, monkeypatch):
    store = Store(tmp_path / "views.sqlite3")
    store.observe_views("abcdefghij1", 1000, 1000)
    monkeypatch.setattr(rising, "search_flat_videos", lambda *a, **k: [{"id": "abcdefghij1", "views": 1004}])
    monkeypatch.setattr(rising, "get_video", lambda vid: {"id": vid, "views": 1004, "published": None})
    result = rising.find_rising(store, clock=lambda: 1060, wait=lambda _: pytest.fail("unnecessary wait"))
    assert result["videos"][0]["measurement"]["views_per_hour"] == 240


def test_rising_search_does_not_substitute_upload_average_for_missing_view_count(tmp_path, monkeypatch):
    monkeypatch.setattr(rising, "search_flat_videos", lambda *a, **k: [{"id": "abcdefghij1", "views": None}])
    monkeypatch.setattr(rising, "get_video", lambda vid: {"id": vid, "views": None, "published": None})
    result = rising.find_rising(Store(tmp_path / "views.sqlite3"), clock=lambda: 1060,
                                wait=lambda _: pytest.fail("nothing to measure"))
    assert result["videos"] == [] and result["measured"] == 0
