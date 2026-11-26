from fastapi.testclient import TestClient

import poc.app as dashboard
from poc.store import Store


def test_search_reports_empty_and_failure(monkeypatch):
    client = TestClient(dashboard.app)
    monkeypatch.setattr(dashboard, "search_videos", lambda query, limit: [])
    assert client.get("/api/search").json() == {"videos": []}

    def fail_search(query, limit):
        raise RuntimeError("조회 실패")

    monkeypatch.setattr(dashboard, "search_videos", fail_search)
    response = client.get("/api/search")
    assert response.status_code == 502
    assert "조회 실패" in response.json()["detail"]


def test_post_requires_grounded_analysis_and_blocks_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "store", Store(tmp_path / "dashboard.sqlite3"))
    monkeypatch.setattr(dashboard, "ANALYSIS_DIR", tmp_path / "analyses")
    monkeypatch.setattr(dashboard, "_run_post", lambda attempt_id: None)
    video_id = "C2LVY7NZeFU"
    comment = "다시 만나면 행복할까 하는 말이 마음에 남네요. 같은 상처가 반복될까 걱정되는 마음이 너무 공감돼요."
    client = TestClient(dashboard.app)
    body = {"video_id": video_id, "comment": comment}
    assert client.post("/api/post", json=body).status_code == 400
    dashboard.analyses[video_id] = {
        "context": {
            "video": {"title": "연애 이야기"},
            "description": "",
            "captions": {"segments": [{"text": "다시 만나면 행복할까?"}]},
        },
        "draft": {"evidence_quote": "다시 만나면 행복할까?"},
    }
    try:
        assert client.post("/api/post", json=body).status_code == 200
        assert client.post("/api/post", json=body).status_code == 409
    finally:
        dashboard.analyses.clear()


def test_failed_deletion_can_be_retried_without_reposting(tmp_path, monkeypatch):
    store = Store(tmp_path / "dashboard.sqlite3")
    monkeypatch.setattr(dashboard, "store", store)
    attempt = store.reserve("C2LVY7NZeFU", "youtube1", "댓글")
    store.stage(attempt, "opening")
    store.stage(attempt, "typing")
    store.begin_send(attempt)
    store.confirm(attempt)
    store.record_deletion(attempt, False, error="삭제 메뉴 오류")
    monkeypatch.setattr(dashboard, "retry_deletion", lambda db, job, headless: db.record_deletion(job, True))
    client = TestClient(dashboard.app)
    assert client.post(f"/api/retry-deletion/{attempt}").status_code == 200
    assert store.get(attempt)["deletion_status"] == "deleted"
    assert store.snapshot()["count"] == 1
    assert client.post(f"/api/retry-deletion/{attempt}").status_code == 400


def test_user_can_supply_missing_video_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "ANALYSIS_DIR", tmp_path / "analyses")
    video_id = "C2LVY7NZeFU"
    dashboard.analyses[video_id] = {
        "context": {
            "video": {"id": video_id, "title": "연애 이야기"},
            "description": "",
            "captions": {"state": "absent", "segments": []},
            "comments": [],
        },
        "draft": {"evidence_quote": "", "grounded": False, "requires_review": True},
    }
    try:
        client = TestClient(dashboard.app)
        response = client.post("/api/evidence", json={"video_id": video_id, "quote": "다시 만나면 행복할까?"})
        assert response.status_code == 200
        assert response.json()["draft"]["evidence_source"] == "사용자 확인"
        assert response.json()["draft"]["grounded"]
        assert dashboard.evidence_is_grounded("다시 만나면 행복할까?", dashboard.analyses[video_id]["context"])
    finally:
        dashboard.analyses.clear()
