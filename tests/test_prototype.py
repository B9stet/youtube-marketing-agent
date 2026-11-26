import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from poc.app import app
import poc.prototype_api as api
import poc.prototype_service as service
from poc.prototype_providers import ProviderError
import poc.prototype_providers as providers
from poc.store import Store


VIDEO_ID = "AbCdEfGhI12"
ORIGINAL = "다시 만나는 거지? 하고 당연하게 묻는 장면이 답답하네. 엇갈린 마음이 남을 때 운명원 연애 사주도 한번 보고 싶다."
REVISED = "다시 만나는 거지? 하고 묻는 모습이 참 답답하네. 서로 다른 마음을 보니 운명원 연애 사주라도 찾아보고 싶어짐."


def context(captions=True):
    return {
        "video": {"id": VIDEO_ID, "title": "다시 만나자는 말", "channel": "영상 채널", "url": f"https://www.youtube.com/watch?v={VIDEO_ID}"},
        "description": "재회에 관한 이야기",
        "captions": {"state": "available" if captions else "absent", "source": "manual" if captions else None,
            "segments": [{"start": "00:00:02.000", "text": "다시 만나는 거지?"},
                         {"start": "00:00:05.000", "text": "아니, 만날 생각이 없어."}] if captions else []},
        "comments": [{"text": "다시 만나는 거냐고 당연히 묻는 게 답답하다", "likes": 12}],
    }


def scores(**overrides):
    from poc.natural_comment import JEV_QUESTIONS
    return {name: overrides.get(name, .1) for name in JEV_QUESTIONS}


def test_scene_candidates_use_captions_and_keep_viewer_comments_separate():
    video = context()
    scene = service.select_scenes(video)[0]
    assert scene["quote"] in [x["text"] for x in video["captions"]["segments"]]
    assert scene["comments"][0]["text"] == video["comments"][0]["text"]
    assert not scene["manual"]
    fallback = service.select_scenes(context(False))[0]
    assert fallback["quote"] == ""
    assert fallback["id"] == "no-caption"


def test_credit_comments_are_not_selected_as_viewer_reactions():
    video = context()
    video["comments"].insert(0, {"text": "Instagram 출연진 장소협찬 안내", "likes": 500})
    scene = service.select_scenes(video)[0]
    assert all("장소협찬" not in comment["text"] for comment in scene["comments"])
    assert scene["comments"][0]["text"] == video["comments"][1]["text"]


def test_repeated_hobby_words_do_not_outrank_relationship_dialogue():
    video = context()
    video["comments"] = []
    video["captions"]["segments"] = [
        {"start": "00:00:05.000", "text": "여행 좋아하고 책도 좋아하고 취미가 좋아요."},
        {"start": "00:02:05.000", "text": "다시 만나서 연애하면 서로 마음이 편할까?"},
        {"start": "00:02:10.000", "text": "재회하고 또 헤어질까 봐 걱정돼요."},
    ]
    assert service.select_scenes(video)[0]["start"] >= 90


def test_integrated_generation_compiles_targeted_correction_and_rediagnoses(monkeypatch):
    calls, stages = [], []
    def generate(settings, prompt, material):
        calls.append((prompt, material))
        if len(calls) == 1:
            return {"answer": {"action": "draft", "comment": ORIGINAL, "concern": "재회에 대한 마음이 엇갈린다."}, "model": "test"}
        return {"answer": {"decision": "rewrite", "comment": REVISED, "reason": ""}, "model": "test"}
    diagnoses = iter([scores(brand_jump=.8), scores()])
    monkeypatch.setattr(service, "generate_json", generate)
    monkeypatch.setattr(service, "diagnose", lambda *args: {"scores": next(diagnoses), "model": "test"})
    video = context()
    result = service.create_draft(video, service.select_scenes(video)[0], "reference", {}, [], stages.append)
    assert result["original"] == ORIGINAL
    assert result["comment"] == REVISED and result["corrected"]
    assert result["review"]["action"] == "review_draft"
    assert "선택된 교정 항목" in calls[1][0] and "운명원 부분" in calls[1][0]
    assert stages == ["generating", "diagnosing", "correcting", "verifying"]


def test_jev_failure_is_exposed_as_review_needed(monkeypatch):
    monkeypatch.setattr(service, "generate_json", lambda *args: {"answer": {"action": "draft", "comment": ORIGINAL, "concern": "재회"}, "model": "test"})
    def unavailable(*args):
        raise ProviderError("진단 연결을 확인해 주세요.")
    monkeypatch.setattr(service, "diagnose", unavailable)
    result = service.create_draft(context(), service.select_scenes(context())[0], "reference", {}, [])
    assert result["comment"] == ORIGINAL
    assert result["review"]["action"] == "review_unavailable"
    assert result["diagnosis"] is None


def test_high_signal_after_the_single_correction_still_requires_review(monkeypatch):
    results = iter([
        {"answer": {"action": "draft", "comment": ORIGINAL, "concern": "재회"}, "model": "test"},
        {"answer": {"decision": "rewrite", "comment": REVISED, "reason": ""}, "model": "test"},
    ])
    monkeypatch.setattr(service, "generate_json", lambda *args: next(results))
    monkeypatch.setattr(service, "diagnose", lambda *args: {"scores": scores(repetition=.8), "model": "test"})
    result = service.create_draft(context(), service.select_scenes(context())[0], "reference", {}, [])
    assert result["corrected"] and result["review"]["action"] == "rewrite"
    assert "직접 확인" in result["review"]["reason"]


def test_explicit_reference_is_recorded_as_the_original_candidate_index(monkeypatch):
    video = context()
    video["comments"].append({"text": "그 장면은 두 사람의 마음이 엇갈려서 안타깝다", "likes": 2})
    scene = service.select_scenes(video)[0]
    seen = {}
    def generate(settings, prompt, material):
        seen.update(material)
        return {"answer": {"action": "draft", "comment": ORIGINAL, "concern": "재회", "reference_index": 0,
            "reference_viewpoint": "선택한 반응의 아쉬움"}, "model": "test"}
    monkeypatch.setattr(service, "generate_json", generate)
    monkeypatch.setattr(service, "diagnose", lambda *args: {"scores": scores(), "model": "test"})
    result = service.create_draft(video, scene, "reference", {}, [], reference_index=1)
    assert len(seen["reference_comments"]) == 1
    assert seen["reference_comments"][0]["text"] == scene["comments"][1]["text"]
    assert result["reference_index"] == 1
    assert result["reference_viewpoint"] == "선택한 반응의 아쉬움"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ANALYSIS_DIR", tmp_path / "analyses")
    monkeypatch.setattr(api, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(api, "store", Store(tmp_path / "test.sqlite3"))
    monkeypatch.setattr(api, "get_context", lambda _: context())
    monkeypatch.setattr(service, "generate_json", lambda *args: {"answer": {"action": "draft", "comment": ORIGINAL, "concern": "재회에 대한 마음이 엇갈린다."}, "model": "test"})
    monkeypatch.setattr(service, "diagnose", lambda *args: {"scores": scores(), "model": "test"})
    called = []
    monkeypatch.setattr(api, "_run_post", lambda attempt_id: called.append(attempt_id))
    with api._job_lock:
        api._jobs.clear()
        api._busy.clear()
        api._review_jobs.clear()
        api._reviews.clear()
    return TestClient(app), called


def wait_job(client, job_id, *, wait_review=True):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/v2/jobs/{job_id}").json()
        if job["stage"] == "failed":
            return job
        if job["stage"] == "complete":
            record = next((value for value in (job.get("result") or {}).get("draft_versions", [])
                           if value.get("id") == job.get("draft_id")), None)
            if not wait_review or not record or not record.get("review_pending"):
                return job
        time.sleep(.01)
    raise AssertionError("분석 작업이 끝나지 않았습니다.")


def test_generation_review_and_post_use_grounded_saved_analysis(isolated):
    client, called = isolated
    response = client.post("/api/v2/jobs", json={"video_id": VIDEO_ID})
    job = wait_job(client, response.json()["job_id"])
    assert job["stage"] == "complete", job
    analysis = client.get(f"/api/v2/analysis/{VIDEO_ID}").json()
    assert analysis["draft"]["comment"] == ORIGINAL
    assert analysis["draft"]["grounded"] and analysis["draft"]["mentions_evidence"]
    edited = ORIGINAL + " 인연은 어렵네."
    assert client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": edited}).status_code == 400
    posted = client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": edited, "review_acknowledged": True})
    assert posted.status_code == 200
    assert posted.json()["delete_after_confirmation"] is True
    assert called == [posted.json()["attempt_id"]]
    assert client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": edited, "review_acknowledged": True}).status_code == 409
    assert client.get("/api/v2/status").json()["count"] == 1


def test_captionless_video_requires_a_manually_confirmed_scene(isolated, monkeypatch):
    client, _ = isolated
    monkeypatch.setattr(api, "get_context", lambda _: context(False))
    job = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])
    assert job["stage"] == "complete"
    assert job["result"]["draft"]["review"]["action"] == "review_facts"
    assert client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": ORIGINAL, "review_acknowledged": True}).status_code == 400
    evidence = client.post("/api/v2/evidence", json={"video_id": VIDEO_ID, "quote": "다시 만나는 거지?"})
    assert evidence.status_code == 200 and evidence.json()["selected_scene"] == "no-caption"
    scene_id = evidence.json()["added_scene_id"]
    job = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID, "scene_id": scene_id}).json()["job_id"])
    candidate = job["result"]["candidate_draft_id"]
    chosen = client.post("/api/v2/drafts/select", json={"video_id": VIDEO_ID, "draft_id": candidate}).json()
    assert chosen["draft"]["grounded"] and chosen["selected_scene"] == scene_id


def test_edited_comment_is_saved_only_after_a_successful_new_review(isolated, monkeypatch):
    client, _ = isolated
    job = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])
    assert job["stage"] == "complete"
    edited = ORIGINAL + " 인연은 어렵네."
    monkeypatch.setattr(api, "diagnose", lambda *args: {"scores": scores(brand_jump=.5), "model": "test"})
    reviewed = client.post("/api/v2/review", json={"video_id": VIDEO_ID, "comment": edited})
    assert reviewed.status_code == 200
    assert reviewed.json()["draft"]["comment"] == edited
    assert reviewed.json()["draft"]["review"]["action"] == "review_signal"
    assert client.get(f"/api/v2/analysis/{VIDEO_ID}").json()["draft"]["comment"] == edited
    assert client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": edited}).status_code == 400
    assert client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": edited, "review_acknowledged": True}).status_code == 200


def test_regeneration_keeps_an_operator_edited_previous_draft(isolated):
    client, _ = isolated
    first = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])
    assert first["stage"] == "complete"
    edited = ORIGINAL + " 내 마음도 복잡하다."
    second = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID, "previous_comment": edited}).json()["job_id"])
    assert second["stage"] == "complete"
    assert edited in second["result"]["previous_drafts"]


def test_generated_text_is_available_before_review_and_edit_survives_correction(isolated, monkeypatch):
    client, _ = isolated
    entered, release = Event(), Event()
    calls = []
    def generate(settings, prompt, material):
        calls.append(1)
        if len(calls) == 1:
            return {"answer": {"action": "draft", "comment": ORIGINAL, "concern": "재회"}, "model": "test"}
        return {"answer": {"decision": "rewrite", "comment": REVISED}, "model": "test"}
    def diagnose(*args):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
            return {"scores": scores(brand_jump=.8), "model": "test"}
        return {"scores": scores(), "model": "test"}
    monkeypatch.setattr(service, "generate_json", generate)
    monkeypatch.setattr(service, "diagnose", diagnose)
    try:
        job_id = client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"]
        assert entered.wait(5)
        mid = client.get(f"/api/v2/jobs/{job_id}").json()
        assert mid["stage"] == "complete"
        assert mid["result"]["draft"]["comment"] == ORIGINAL
        assert mid["result"]["draft"]["review_pending"]
        draft_id = mid["result"]["draft"]["id"]
        assert client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": ORIGINAL,
                           "review_acknowledged": True}).status_code == 409
        edited = ORIGINAL + " 내 마음도 복잡하다."
        saved = client.post("/api/v2/drafts/edit", json={"video_id": VIDEO_ID, "draft_id": draft_id,
                            "comment": edited, "expected_revision": 0})
        assert saved.status_code == 200 and saved.json()["edit_revision"] == 1
        release.set()
        final = wait_job(client, job_id)["result"]["draft"]
        assert final["comment"] == edited
        assert final["diagnosis_text"] == ORIGINAL
        assert final["correction_suggestion"]["comment"] == REVISED
        assert client.post("/api/v2/drafts/correction", json={"video_id": VIDEO_ID, "draft_id": draft_id,
                            "expected_revision": 1}).status_code == 409
    finally:
        release.set()


def test_new_generation_can_start_while_earlier_draft_is_being_reviewed(isolated, monkeypatch):
    client, _ = isolated
    entered, release = Event(), Event()
    count = []
    def diagnose(*args):
        count.append(1)
        if len(count) == 1:
            entered.set()
            assert release.wait(5)
        return {"scores": scores(), "model": "test"}
    monkeypatch.setattr(service, "diagnose", diagnose)
    try:
        first_id = client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"]
        assert entered.wait(5)
        assert wait_job(client, first_id, wait_review=False)["result"]["draft"]["review_pending"]
        second = client.post("/api/v2/jobs", json={"video_id": VIDEO_ID, "tone": "casual"})
        assert second.status_code == 200
        second_id = second.json()["job_id"]
        generated = wait_job(client, second_id, wait_review=False)
        assert generated["result"]["candidate_draft_id"] == second_id
        release.set()
        assert not wait_job(client, first_id)["result"]["draft"]["review_pending"]
        wait_job(client, second_id)
    finally:
        release.set()


def test_generation_remains_available_when_review_workers_are_all_busy(isolated, monkeypatch):
    client, _ = isolated
    entered, release = Event(), Event()
    calls = []
    def diagnose(*args):
        calls.append(1)
        if len(calls) >= 3:
            entered.set()
        assert release.wait(5)
        return {"scores": scores(), "model": "test"}
    monkeypatch.setattr(service, "diagnose", diagnose)
    jobs = []
    try:
        for _ in range(3):
            response = client.post("/api/v2/jobs", json={"video_id": VIDEO_ID})
            assert response.status_code == 200
            jobs.append(response.json()["job_id"])
            assert wait_job(client, jobs[-1], wait_review=False)["stage"] == "complete"
        assert entered.wait(5)
        fourth = client.post("/api/v2/jobs", json={"video_id": VIDEO_ID})
        assert fourth.status_code == 200
        jobs.append(fourth.json()["job_id"])
        fourth_result = wait_job(client, jobs[-1], wait_review=False)
        assert fourth_result["stage"] == "complete"
        queued = next(value for value in fourth_result["result"]["draft_versions"] if value["id"] == jobs[-1])
        assert queued["review_stage"] == "review_queued"
    finally:
        release.set()
    for job_id in jobs:
        assert wait_job(client, job_id)["stage"] == "complete"


def test_accepting_correction_creates_a_separate_reviewed_version(isolated, monkeypatch):
    client, _ = isolated
    results = iter([
        {"answer": {"action": "draft", "comment": ORIGINAL, "concern": "재회"}, "model": "test"},
        {"answer": {"decision": "rewrite", "comment": REVISED}, "model": "test"},
    ])
    assessments = iter([scores(brand_jump=.8), scores()])
    monkeypatch.setattr(service, "generate_json", lambda *args: next(results))
    monkeypatch.setattr(service, "diagnose", lambda *args: {"scores": next(assessments), "model": "test"})
    result = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])["result"]
    first_id = result["active_draft_id"]
    assert result["draft"]["comment"] == ORIGINAL and result["draft"]["correction_suggestion"]["comment"] == REVISED
    response = client.post("/api/v2/drafts/correction", json={"video_id": VIDEO_ID, "draft_id": first_id, "expected_revision": 0})
    assert response.status_code == 200
    value = response.json()
    assert value["draft"]["comment"] == REVISED and value["draft"]["diagnosis_text"] == REVISED
    assert value["active_draft_id"] != first_id
    assert next(x for x in value["draft_versions"] if x["id"] == first_id)["comment"] == ORIGINAL


def test_new_generation_preserves_current_version_and_uses_all_selected_settings(isolated, monkeypatch):
    client, _ = isolated
    seen = []
    def generate(settings, prompt, material):
        seen.append(material)
        return {"answer": {"action": "draft", "comment": ORIGINAL, "concern": "재회", "reference_index": 0}, "model": "test"}
    monkeypatch.setattr(service, "generate_json", generate)
    first = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])["result"]
    first_id = first["active_draft_id"]
    edited = ORIGINAL + " 내 마음도 복잡하다."
    assert client.post("/api/v2/drafts/edit", json={"video_id": VIDEO_ID, "draft_id": first_id,
                       "comment": edited, "expected_revision": 0}).status_code == 200
    second_job = client.post("/api/v2/jobs", json={"video_id": VIDEO_ID, "perspective": "opinion",
                            "tone": "casual", "spelling": True, "reference_index": 0}).json()["job_id"]
    second = wait_job(client, second_job)["result"]
    assert second["active_draft_id"] == first_id and second["draft"]["comment"] == edited
    candidate = second["candidate_draft_id"]
    assert candidate != first_id and second["draft_versions"][-1]["tone"] == "casual"
    assert "구어체" in seen[-1]["voice"] and "오타" in seen[-1]["voice"]
    assert len(seen[-1]["reference_comments"]) == 1
    selected = client.post("/api/v2/drafts/select", json={"video_id": VIDEO_ID, "draft_id": candidate}).json()
    assert selected["active_draft_id"] == candidate and selected["perspective"] == "opinion"
    original = next(v for v in selected["draft_versions"] if v["id"] == first_id)
    assert original["comment"] == edited


def test_review_result_stays_attached_to_text_examined_during_later_edit(isolated, monkeypatch):
    client, _ = isolated
    first = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])["result"]
    draft_id = first["active_draft_id"]
    entered, release = Event(), Event()
    def diagnose(*args):
        entered.set()
        assert release.wait(5)
        return {"scores": scores(), "model": "test"}
    monkeypatch.setattr(api, "diagnose", diagnose)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, "/api/v2/review", json={"video_id": VIDEO_ID, "draft_id": draft_id, "comment": ORIGINAL})
            assert entered.wait(5)
            edited = ORIGINAL + " 인연은 어렵네."
            assert client.post("/api/v2/drafts/edit", json={"video_id": VIDEO_ID, "draft_id": draft_id,
                               "comment": edited, "expected_revision": 0}).status_code == 200
            release.set()
            response = future.result(timeout=5)
        value = response.json()["draft"]
        assert value["comment"] == edited and value["diagnosis_text"] == ORIGINAL
        assert client.post("/api/v2/drafts/edit", json={"video_id": VIDEO_ID, "draft_id": draft_id,
                           "comment": ORIGINAL, "expected_revision": 0}).status_code == 409
    finally:
        release.set()


def test_manually_rediagnosed_comment_can_get_its_own_correction_suggestion(isolated, monkeypatch):
    client, _ = isolated
    first = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])["result"]
    draft_id = first["active_draft_id"]
    edited = ORIGINAL + " 인연은 어렵네."
    assert client.post("/api/v2/drafts/edit", json={"video_id": VIDEO_ID, "draft_id": draft_id,
                       "comment": edited, "expected_revision": 0}).status_code == 200
    monkeypatch.setattr(api, "diagnose", lambda *args: {"scores": scores(brand_jump=.8), "model": "test"})
    reviewed = client.post("/api/v2/review", json={"video_id": VIDEO_ID, "draft_id": draft_id, "comment": edited}).json()
    assert reviewed["draft"]["review"]["action"] == "rewrite"
    entered, release = Event(), Event()
    def correct(settings, prompt, material):
        assert material["comment"] == edited and "선택된 교정 항목" in prompt
        entered.set()
        assert release.wait(5)
        return {"answer": {"decision": "rewrite", "comment": REVISED}, "model": "test"}
    monkeypatch.setattr(api, "generate_json", correct)
    try:
        started = client.post("/api/v2/drafts/correct", json={"video_id": VIDEO_ID, "draft_id": draft_id, "expected_revision": 1})
        assert started.status_code == 200 and entered.wait(5)
        newer = edited + " 더 생각해봐야겠다."
        assert client.post("/api/v2/drafts/edit", json={"video_id": VIDEO_ID, "draft_id": draft_id,
                           "comment": newer, "expected_revision": 1}).status_code == 200
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = client.get(f"/api/v2/analysis/{VIDEO_ID}").json()["draft"]
            if not current["review_pending"]:
                break
            time.sleep(.01)
        assert current["comment"] == newer
        assert current["correction_suggestion"]["source_text"] == edited
        assert current["correction_suggestion"]["comment"] == REVISED
        assert client.post("/api/v2/drafts/correction", json={"video_id": VIDEO_ID, "draft_id": draft_id,
                           "expected_revision": 2}).status_code == 409
    finally:
        release.set()


def test_optional_variant_preview_does_not_change_stored_draft(isolated):
    client, _ = isolated
    result = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])["result"]
    text = "다시 만나는 거지? 하는 말은 나도 고민될 것 같아요. 그런 마음이 드니 운명원 연애 사주도 한번 찾아보고 싶다."
    preview = client.post("/api/v2/drafts/variant", json={"video_id": VIDEO_ID, "draft_id": result["active_draft_id"], "comment": text})
    assert preview.status_code == 200 and preview.json()["applied"]
    assert "것같" in preview.json()["comment"]
    assert client.get(f"/api/v2/analysis/{VIDEO_ID}").json()["draft"]["comment"] == ORIGINAL


def test_legacy_analysis_is_preserved_as_a_version_when_edited(isolated):
    client, _ = isolated
    scene = service.select_scenes(context())[0]
    legacy = {"context": context(), "scenes": [scene], "selected_scene": scene["id"], "perspective": "reference",
              "draft": {"action": "draft", "comment": ORIGINAL, "review": {"action": "review_draft"}},
              "previous_drafts": [REVISED]}
    api.ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    api._analysis_path(VIDEO_ID).write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    result = client.get(f"/api/v2/analysis/{VIDEO_ID}").json()
    assert result["draft"]["comment"] == ORIGINAL and result["active_draft_id"] == "legacy-" + VIDEO_ID
    edited = ORIGINAL + " 인연은 어렵네."
    assert client.post("/api/v2/drafts/edit", json={"video_id": VIDEO_ID, "draft_id": result["active_draft_id"],
                       "comment": edited, "expected_revision": 0}).status_code == 200
    saved = client.get(f"/api/v2/analysis/{VIDEO_ID}").json()
    assert saved["draft"]["comment"] == edited and saved["previous_drafts"] == [REVISED]


def test_interrupted_review_can_be_retried_after_restart(isolated):
    client, _ = isolated
    wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])
    saved = api._load(VIDEO_ID)
    saved["draft"]["review_pending"] = True
    api._save(VIDEO_ID, saved)
    restored = client.get(f"/api/v2/analysis/{VIDEO_ID}").json()["draft"]
    assert not restored["review_pending"] and restored["review"]["action"] == "review_unavailable"


def test_reviewing_an_unchanged_correction_keeps_its_original_and_usage(isolated, monkeypatch):
    client, _ = isolated
    wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])
    analysis = api._load(VIDEO_ID)
    analysis["draft"]["corrected"] = True
    analysis["draft"]["original"] = REVISED
    api._save(VIDEO_ID, analysis)
    monkeypatch.setattr(api, "diagnose", lambda *args: {"scores": scores(repetition=.8), "model": "test", "usage": {"input_tokens": 10}})
    reviewed = client.post("/api/v2/review", json={"video_id": VIDEO_ID, "comment": ORIGINAL}).json()
    assert reviewed["draft"]["corrected"] and reviewed["draft"]["original"] == REVISED
    assert reviewed["draft"]["operations"][-1]["step"] == "manual_review"
    assert reviewed["draft"]["operations"][-1]["usage"]["input_tokens"] == 10
    assert "직접 확인" in reviewed["draft"]["review"]["reason"]


def test_settings_save_independent_provider_choices_without_credentials(isolated):
    client, _ = isolated
    settings = client.get("/api/v2/settings").json()
    settings.update({"generation_provider": "gcp", "generation_model": "gemini-model", "jev_provider": "official", "jev_model": "jev-latest",
                     "gcp_project": "test-project", "gcp_region": "global"})
    saved = client.put("/api/v2/settings", json=settings)
    assert saved.status_code == 200
    result = client.get("/api/v2/settings").json()
    assert result["generation_provider"] == "gcp" and result["jev_provider"] == "official"
    assert result["generation_models"]["gcp"] == "gemini-model"
    assert not any("key" in field.lower() for field in result)


def test_session_credential_is_not_echoed_or_saved_and_can_be_cleared(isolated, monkeypatch):
    client, _ = isolated
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    providers.clear_session_credential()
    fake = "test-only-secret-value"
    try:
        saved = client.put("/api/v2/credentials", json={"provider": "openai", "key": fake})
        assert saved.status_code == 200 and saved.json()["openai"] == "session"
        assert fake not in saved.text and fake not in client.get("/api/v2/settings").text
        settings = client.get("/api/v2/settings").json()
        assert client.put("/api/v2/settings", json=settings).status_code == 200
        assert fake not in api.SETTINGS_FILE.read_text(encoding="utf-8")
        bad = client.put("/api/v2/credentials", json={"provider": "openai", "key": fake + "\n"})
        assert bad.status_code == 400 and fake not in bad.text
        assert client.delete("/api/v2/credentials").json()["openai"] == "missing"
    finally:
        providers.clear_session_credential()


def test_session_credential_is_used_for_the_selected_provider_without_a_live_call(monkeypatch):
    fake = "test-only-secret-value"
    calls = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"output_text": '{"comment":"테스트"}', "model": "mock"}
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, headers, json):
            calls.append((url, headers.get("Authorization")))
            return Response()
    monkeypatch.setattr(providers.httpx, "Client", Client)
    providers.set_session_credential(fake)
    try:
        result = providers.generate_json({"generation_provider": "openai", "generation_model": "mock"}, "p", {})
        assert result["answer"]["comment"] == "테스트"
        assert calls == [("https://api.openai.com/v1/responses", "Bearer " + fake)]
    finally:
        providers.clear_session_credential()


def test_rising_job_reuses_running_job_and_persists_result(isolated, tmp_path, monkeypatch):
    client, _ = isolated
    monkeypatch.setattr(api, "RISING_FILE", tmp_path / "rising.json")
    entered, release = Event(), Event()
    def fake_find(store, progress):
        progress(stage="observing", remaining_seconds=60)
        entered.set()
        assert release.wait(5)
        return {"videos": [], "searched": 3, "checked": 2, "measured": 1}
    monkeypatch.setattr(api, "find_rising", fake_find)
    with api._job_lock:
        api._rising_jobs.clear()
    try:
        first = client.post("/api/v2/rising/jobs").json()["job_id"]
        assert entered.wait(5)
        assert client.post("/api/v2/rising/jobs").json()["job_id"] == first
        assert client.get("/api/v2/rising/active").json()["id"] == first
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/api/v2/rising/jobs/{first}").json()
            if job["stage"] in ("complete", "failed"):
                break
            time.sleep(.01)
        assert job["stage"] == "complete"
        assert client.get("/api/v2/rising/latest").json()["result"]["searched"] == 3
    finally:
        release.set()


def test_profile_login_window_opens_only_requested_profile(isolated, monkeypatch):
    client, _ = isolated
    called = []
    monkeypatch.setattr(api, "open_profile", called.append)
    assert client.post("/api/v2/profiles/youtube2/open").status_code == 200
    assert client.post("/api/v2/profiles/auto/open").status_code == 400
    assert called == ["youtube2"]


def test_previously_draft_only_rising_video_can_be_posted(isolated, tmp_path, monkeypatch):
    client, called = isolated
    with api.store._connect() as db:
        db.execute("CREATE TABLE draft_only_videos (video_id TEXT PRIMARY KEY, source TEXT NOT NULL, identified_at TEXT NOT NULL)")
        db.execute("INSERT INTO draft_only_videos VALUES (?, ?, ?)", (VIDEO_ID, "rising", "2026-11-26"))
    saved = tmp_path / "rising.json"
    saved.write_text(json.dumps({"result": {"videos": [{"id": VIDEO_ID, "draft_only": True}],
                                           "searched": 1, "checked": 1, "measured": 1}}), encoding="utf-8")
    monkeypatch.setattr(api, "RISING_FILE", saved)
    latest = client.get("/api/v2/rising/latest").json()["result"]["videos"][0]
    assert latest["id"] == VIDEO_ID and "draft_only" not in latest
    job = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])
    assert job["stage"] == "complete"
    response = client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": ORIGINAL, "profile": "auto", "review_acknowledged": True})
    assert response.status_code == 200 and response.json()["profile"] == "youtube1"
    assert response.json()["delete_after_confirmation"] is True
    assert api.store.get(called[0])["video_id"] == VIDEO_ID
    duplicate = client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": ORIGINAL, "profile": "youtube2", "review_acknowledged": True})
    assert duplicate.status_code == 409 and len(called) == 1


def test_rising_video_post_button_uses_normal_publish_flow(isolated, tmp_path, monkeypatch):
    client, called = isolated
    with api.store._connect() as db:
        db.execute("CREATE TABLE draft_only_videos (video_id TEXT PRIMARY KEY, source TEXT NOT NULL, identified_at TEXT NOT NULL)")
        db.execute("INSERT INTO draft_only_videos VALUES (?, ?, ?)", (VIDEO_ID, "rising", "2026-11-26"))
    saved = tmp_path / "rising-ui.json"
    video = {**context()["video"], "views": 1003, "draft_only": True,
             "measurement": {"end_at": 1060, "elapsed_seconds": 60, "views_added": 3, "views_per_hour": 180}}
    saved.write_text(json.dumps({"result": {"videos": [video], "searched": 1, "checked": 1, "measured": 1}}), encoding="utf-8")
    monkeypatch.setattr(api, "RISING_FILE", saved)
    job = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])
    assert job["stage"] == "complete"

    def route_request(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.netloc != "prototype.test":
            route.abort()
            return
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        response = client.request(request.method, path, content=request.post_data,
                                  headers={"content-type": request.headers.get("content-type", "application/json")})
        body = response.content
        # Older stored or cached results may still include the retired flag.
        if parsed.path in ("/api/v2/rising/latest", f"/api/v2/analysis/{VIDEO_ID}", "/api/v2/status"):
            data = response.json()
            if parsed.path == "/api/v2/rising/latest":
                data["result"]["videos"][0]["draft_only"] = True
            elif parsed.path.startswith("/api/v2/analysis/"):
                data["draft_only"] = True
            else:
                data["draft_only_videos"] = [VIDEO_ID]
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        route.fulfill(status=response.status_code, content_type=response.headers.get("content-type", "application/json"), body=body)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route("**/*", route_request)
        page.goto("http://prototype.test/")
        page.locator("#videoList .video-item").first.click()
        expect(page.locator("#postButton")).to_be_enabled()
        assert "초안만" not in page.locator("#findRising").inner_text()
        assert "초안만" not in page.locator("#risingStatus").inner_text()
        assert "초안만" not in page.locator("#videoList .video-item").first.inner_text()
        page.select_option("#profileSelect", "youtube3")
        expect(page.locator("#postLabel")).to_have_text("유튜브3로 댓글 달기")
        with page.expect_response("**/api/v2/post") as posted:
            page.locator("#postButton").click()
        assert posted.value.status == 200
        assert called and api.store.get(called[0])["profile"] == "youtube3"
        browser.close()


def test_profile_selection_and_automatic_assignment_are_returned(isolated):
    client, called = isolated
    job = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])
    assert job["stage"] == "complete"
    response = client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": ORIGINAL, "profile": "youtube3"})
    assert response.status_code == 200 and response.json()["profile"] == "youtube3"
    assert api.store.get(called[0])["profile"] == "youtube3"


def test_post_rejects_a_different_draft_or_outdated_revision(isolated):
    client, called = isolated
    result = wait_job(client, client.post("/api/v2/jobs", json={"video_id": VIDEO_ID}).json()["job_id"])["result"]
    draft_id = result["active_draft_id"]
    wrong = client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": ORIGINAL,
                       "draft_id": "another-draft", "expected_revision": 0, "review_acknowledged": True})
    assert wrong.status_code == 409
    stale = client.post("/api/v2/post", json={"video_id": VIDEO_ID, "comment": ORIGINAL,
                       "draft_id": draft_id, "expected_revision": 1, "review_acknowledged": True})
    assert stale.status_code == 409 and not called


def test_jev_connections_use_separate_endpoints_and_credentials(monkeypatch):
    calls = []
    monkeypatch.setenv("EXPERIENTIAL_API_KEY", "gateway-test-token")
    monkeypatch.setenv("JEV_OFFICIAL_API_KEY", "official-test-token")
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"model": "test", "answers": {key: {"noul": .1} for key in providers.JEV_QUESTIONS}}
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, headers, json):
            calls.append((url, headers["Authorization"], json["model"]))
            return Response()
    monkeypatch.setattr(providers.httpx, "Client", Client)
    providers.diagnose({"jev_provider": "existing", "jev_model": "jev-latest:free"}, {}, ORIGINAL, [])
    providers.diagnose({"jev_provider": "official", "jev_model": "jev-latest"}, {}, ORIGINAL, [])
    assert calls == [
        ("https://api.experientiallabs.ai/v1/systemone", "Bearer gateway-test-token", "jev-latest:free"),
        ("https://api.typesafe.ai/v1/systemone", "Bearer official-test-token", "jev-latest"),
    ]
