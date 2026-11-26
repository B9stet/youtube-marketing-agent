"""Local prototype API: real video analysis, saved drafts, and reviewed posting."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .browser import post_comment
from .config import OUTPUT_DIR, STATE_DIR, PROFILES
from .drafts import character_count, comment_mentions_evidence, evidence_is_grounded
from .natural_comment import compile_feedback, route_signals, validate_integrated
from .prototype_providers import (ProviderError, diagnose, set_session_credential,
                                  clear_session_credential, credential_status, generate_json)
from .prototype_service import PERSPECTIVES, generate_initial_draft, review_generated_draft, manual_scene, select_scenes
from .natural_variants import apply_variant
from .login import open_profile
from .rising import find_rising
from .store import LimitError, Store
from .youtube import get_context, normalize_video_id

router = APIRouter(prefix="/api/v2")
store = Store()
PROTOTYPE_DIR = STATE_DIR / "prototype"
ANALYSIS_DIR = PROTOTYPE_DIR / "analyses"
SETTINGS_FILE = PROTOTYPE_DIR / "settings.json"
RISING_FILE = PROTOTYPE_DIR / "rising-latest.json"
_file_lock = Lock()
_job_lock = Lock()
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="prototype")
_review_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="prototype-review")
_jobs: dict[str, dict] = {}
_rising_jobs: dict[str, dict] = {}
_busy: set[str] = set()
_reviews: set[tuple[str, str]] = set()
_review_jobs: set[str] = set()


class Settings(BaseModel):
    profile: Literal["youtube1", "youtube2", "youtube3", "auto"] = "youtube1"
    generation_provider: Literal["local", "openai", "gcp"] = "local"
    generation_model: str = Field(default="google-vertex/gemini-4-hydrogen", max_length=120)
    jev_provider: Literal["existing", "official"] = "existing"
    jev_model: str = Field(default="jev-latest:free", max_length=120)
    gcp_project: str = Field(default="", max_length=128)
    gcp_region: str = Field(default="global", max_length=40)
    generation_models: dict[str, str] = Field(default_factory=lambda: {
        "local": "google-vertex/gemini-4-hydrogen", "openai": "", "gcp": ""})
    jev_models: dict[str, str] = Field(default_factory=lambda: {"existing": "jev-latest:free", "official": "jev-latest"})


class StartRequest(BaseModel):
    video_id: str
    scene_id: str | None = None
    perspective: Literal["reference", "immersed", "experience", "opinion"] = "reference"
    tone: Literal["natural", "casual"] = "natural"
    spelling: bool = False
    reference_index: int | None = Field(default=None, ge=0, le=2)
    previous_comment: str = Field(default="", max_length=1000)
    refresh: bool = False


class EvidenceRequest(BaseModel):
    video_id: str
    quote: str = Field(min_length=4, max_length=500)


class PostRequest(BaseModel):
    video_id: str
    comment: str = Field(max_length=1000)
    draft_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    profile: Literal["youtube1", "youtube2", "youtube3", "auto"] = "youtube1"
    review_acknowledged: bool = False


class ReviewRequest(BaseModel):
    video_id: str
    comment: str = Field(min_length=1, max_length=1000)
    draft_id: str | None = None


class DraftEditRequest(BaseModel):
    video_id: str
    draft_id: str
    comment: str = Field(max_length=1000)
    expected_revision: int = Field(ge=0)


class DraftSelectRequest(BaseModel):
    video_id: str
    draft_id: str
    expected_active_id: str | None = None


class CorrectionRequest(BaseModel):
    video_id: str
    draft_id: str
    expected_revision: int = Field(ge=0)


class VariantRequest(BaseModel):
    video_id: str
    draft_id: str
    comment: str = Field(max_length=1000)


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _settings() -> dict:
    with _file_lock:
        if SETTINGS_FILE.is_file():
            try:
                return Settings.model_validate_json(SETTINGS_FILE.read_text(encoding="utf-8")).model_dump()
            except (ValueError, OSError):
                pass
    return Settings().model_dump()


def _analysis_path(video_id: str) -> Path:
    return ANALYSIS_DIR / f"{normalize_video_id(video_id)}.json"


def _ensure_versions(analysis: dict) -> dict:
    """Read older saved analyses without discarding their existing draft."""
    versions = analysis.setdefault("draft_versions", {})
    if not versions and analysis.get("draft"):
        video_id = analysis["context"]["video"]["id"]
        version_id = "legacy-" + video_id
        value = analysis["draft"]
        value.setdefault("id", version_id)
        value.setdefault("scene_id", analysis.get("selected_scene"))
        value.setdefault("perspective", analysis.get("perspective", "reference"))
        value.setdefault("tone", analysis.get("tone", "natural"))
        value.setdefault("spelling", analysis.get("spelling", False))
        value.setdefault("source_text", value.get("comment", ""))
        value.setdefault("diagnosis_text", value.get("comment") if value.get("diagnosis") else None)
        value.setdefault("edit_revision", 0)
        versions[version_id] = value
        analysis["active_draft_id"] = version_id
    active = versions.get(analysis.get("active_draft_id"))
    if active:
        analysis.update(draft=active, selected_scene=active.get("scene_id"),
                        perspective=active.get("perspective", "reference"),
                        tone=active.get("tone", "natural"), spelling=active.get("spelling", False))
    return analysis


def _read_analysis_unlocked(video_id: str) -> dict | None:
    try:
        data = json.loads(_analysis_path(video_id).read_text(encoding="utf-8"))
        return _ensure_versions(data) if data.get("context", {}).get("video", {}).get("id") == video_id else None
    except (FileNotFoundError, OSError, ValueError, AttributeError):
        return None


def _load(video_id: str) -> dict | None:
    with _file_lock:
        return _read_analysis_unlocked(video_id)


def _save(video_id: str, value: dict) -> None:
    with _file_lock:
        value["analysis_revision"] = int(value.get("analysis_revision", 0)) + 1
        _write_json(_analysis_path(video_id), _ensure_versions(value))


def _change_analysis(video_id: str, update) -> dict:
    """Apply one read/modify/write while background work and edits can overlap."""
    with _file_lock:
        analysis = _read_analysis_unlocked(video_id)
        if not analysis:
            raise HTTPException(404, "저장된 영상 분석이 없습니다.")
        update(analysis)
        analysis["analysis_revision"] = int(analysis.get("analysis_revision", 0)) + 1
        _ensure_versions(analysis)
        _write_json(_analysis_path(video_id), analysis)
        return analysis


def _public(analysis: dict) -> dict:
    context = analysis["context"]
    return {"video": context["video"],
        "caption_state": context.get("captions", {}).get("state", "absent"),
        "caption_error": context.get("captions", {}).get("error"), "scenes": analysis.get("scenes", []),
        "selected_scene": analysis.get("selected_scene"), "perspective": analysis.get("perspective", "reference"),
        "tone": analysis.get("tone", "natural"), "spelling": analysis.get("spelling", False),
        "draft": analysis.get("draft"), "previous_drafts": analysis.get("previous_drafts", []),
        "active_draft_id": analysis.get("active_draft_id"), "candidate_draft_id": analysis.get("candidate_draft_id"),
        "analysis_revision": analysis.get("analysis_revision", 0),
        "draft_versions": list(analysis.get("draft_versions", {}).values()),
        "perspectives": [{"id": name, "label": item["label"]} for name, item in PERSPECTIVES.items()]}


def _recent_comments(current_id: str, analysis: dict) -> list[str]:
    result = [item for item in analysis.get("previous_drafts", []) if isinstance(item, str)]
    for path in sorted(ANALYSIS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True) if ANALYSIS_DIR.exists() else []:
        if path.stem == current_id:
            continue
        row = _load(path.stem)
        value = ((row or {}).get("draft") or {}).get("comment", "")
        if value:
            result.append(value)
        if len(result) >= 4:
            break
    return result[-4:]


def _set_job(job_id: str, **values) -> None:
    with _job_lock:
        if job_id in _jobs:
            _jobs[job_id].update(values)


def _finish_job(job_id: str, video_id: str, **values) -> None:
    with _job_lock:
        _busy.discard(video_id)
        if job_id in _jobs:
            _jobs[job_id].update(values)


@contextmanager
def _claim_video(video_id: str):
    with _job_lock:
        if video_id in _busy:
            raise HTTPException(409, "이 영상의 다른 작업이 진행 중입니다.")
        _busy.add(video_id)
    try:
        yield
    finally:
        with _job_lock:
            _busy.discard(video_id)


def _run_job(job_id: str, request: StartRequest, video_id: str, settings: dict) -> None:
    generated_id = job_id
    try:
        analysis = _load(video_id)
        if not analysis or request.refresh:
            _set_job(job_id, stage="collecting")
            context = get_context(video_id)
            _set_job(job_id, stage="selecting")
            scenes = select_scenes(context)
            if not analysis:
                analysis = {"context": context, "scenes": scenes, "selected_scene": scenes[0]["id"],
                    "perspective": request.perspective, "draft": None, "previous_drafts": [], "draft_versions": {}}
                _save(video_id, analysis)
            else:
                def replace_sources(current):
                    old_scenes = current.get("scenes", [])
                    current["context"] = context
                    current["scenes"] = scenes + [x for x in old_scenes if x["id"] not in {s["id"] for s in scenes}]
                analysis = _change_analysis(video_id, replace_sources)
        scene_id = request.scene_id or analysis.get("selected_scene") or analysis["scenes"][0]["id"]
        scene = next((x for x in analysis["scenes"] if x["id"] == scene_id), None)
        if not scene:
            raise ValueError("선택한 영상 구간을 찾지 못했습니다.")
        old_comment = (analysis.get("draft") or {}).get("comment", "")
        edited_comment = request.previous_comment.strip()
        previous = _recent_comments(video_id, analysis)
        if old_comment:
            previous.append(old_comment)
        if edited_comment and edited_comment != old_comment:
            previous.append(edited_comment)
        def report_stage(stage: str) -> None:
            with _job_lock:
                in_review = generated_id in _review_jobs
            if not in_review:
                _set_job(job_id, stage=stage)
                return
            def update(current):
                record = current["draft_versions"].get(generated_id)
                if record:
                    record["review_stage"] = stage
            _change_analysis(video_id, update)

        def publish(phase: str, payload: dict) -> None:
            if phase == "generated" and payload.get("action") == "draft":
                with _job_lock:
                    _review_jobs.add(generated_id)
            def update(current):
                versions = current["draft_versions"]
                if phase == "generated":
                    value = deepcopy(payload)
                    value.update({"id": generated_id, "scene_id": scene_id, "perspective": request.perspective,
                        "tone": request.tone, "spelling": request.spelling,
                        "source_text": value.get("comment", ""), "diagnosis_text": None,
                        "edit_revision": 0, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "review_pending": value.get("action") == "draft", "review_stage": "review_queued"})
                    versions[generated_id] = value
                    if current.get("active_draft_id"):
                        current["candidate_draft_id"] = generated_id
                    else:
                        current["active_draft_id"] = generated_id
                    prior = list(current.get("previous_drafts", []))
                    for text in (old_comment, edited_comment):
                        if text and text != value.get("comment") and text not in prior:
                            prior.append(text)
                    current["previous_drafts"] = prior[-10:]
                elif generated_id in versions:
                    versions[generated_id].update(deepcopy(payload))
            latest = _change_analysis(video_id, update)
            public = _public(latest)
            if phase == "generated":
                with _job_lock:
                    _busy.discard(video_id)
                    if job_id in _jobs:
                        _jobs[job_id].update(stage="complete", result=public, error=None, draft_id=generated_id)
            else:
                _set_job(job_id, result=public, draft_id=generated_id)

        initial = generate_initial_draft(analysis["context"], scene, request.perspective, settings, previous,
            report_stage, request.tone, request.spelling, request.reference_index,
            publish=publish)
        if initial.get("action") != "draft":
            return

        def run_review():
            try:
                result = review_generated_draft(initial, scene, settings, previous, report_stage, publish)
                def finish(current):
                    record = current["draft_versions"].get(generated_id)
                    if not record:
                        raise ValueError("생성한 시안을 찾지 못했습니다.")
                    record["review_pending"] = False
                    record["review_stage"] = "complete"
                    record["operations"] = result.get("operations", record.get("operations", []))
                    if result.get("original_review") is not None:
                        record["review"] = result["original_review"]
                        record["diagnosis"] = result.get("original_diagnosis")
                        record["diagnosis_text"] = record.get("source_text") if record["diagnosis"] else None
                        if record["diagnosis"] and result.get("review", {}).get("action") == "review_unavailable":
                            record["correction_error"] = result["review"].get("reason", "교정 요청을 완료하지 못했습니다.")
                    else:
                        record["review"] = result.get("review", record.get("review"))
                    if result.get("corrected") and result.get("comment") != record.get("source_text"):
                        record["correction_suggestion"] = {"comment": result["comment"],
                            "source_text": record.get("source_text"), "diagnosis": result.get("diagnosis"),
                            "review": result.get("review"), "mentions_evidence": result.get("mentions_evidence"),
                            "model": result.get("model")}
                latest = _change_analysis(video_id, finish)
                with _job_lock:
                    _review_jobs.discard(generated_id)
                    if job_id in _jobs:
                        _jobs[job_id].update(result=_public(latest), error=None, draft_id=generated_id)
            except (ProviderError, ValueError) as exc:
                _fail_generation(job_id, video_id, generated_id, str(exc)[:300])
            except Exception as exc:
                _fail_generation(job_id, video_id, generated_id, f"검토에 실패했습니다. ({type(exc).__name__}: {str(exc)[:180]})")

        _review_executor.submit(run_review)
    except (ProviderError, ValueError) as exc:
        _fail_generation(job_id, video_id, generated_id, str(exc)[:300])
    except Exception as exc:
        _fail_generation(job_id, video_id, generated_id, f"영상 처리에 실패했습니다. ({type(exc).__name__}: {str(exc)[:180]})")


def _fail_generation(job_id: str, video_id: str, draft_id: str, message: str) -> None:
    with _job_lock:
        already_generated = _jobs.get(job_id, {}).get("stage") == "complete"
        if not already_generated:
            _review_jobs.discard(draft_id)
    try:
        def mark(current):
            record = current["draft_versions"].get(draft_id)
            if record and record.get("review_pending"):
                record["review_pending"] = False
                record["review_stage"] = "failed"
                record["review"] = {"action": "review_unavailable", "reason": message}
        analysis = _change_analysis(video_id, mark)
        if already_generated:
            with _job_lock:
                _review_jobs.discard(draft_id)
                if job_id in _jobs:
                    _jobs[job_id].update(result=_public(analysis), review_error=message)
        else:
            _finish_job(job_id, video_id, stage="failed", error=message, result=_public(analysis), draft_id=draft_id)
    except HTTPException:
        if already_generated:
            with _job_lock:
                _review_jobs.discard(draft_id)
                if job_id in _jobs:
                    _jobs[job_id]["review_error"] = message
        else:
            _finish_job(job_id, video_id, stage="failed", error=message)


@router.get("/settings")
def get_settings():
    return _settings()


@router.get("/credentials")
def get_credentials():
    return credential_status()


@router.put("/credentials")
async def save_credentials(request: Request):
    # Parse manually so a validation error can never echo submitted credentials.
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or payload.get("provider") != "openai":
            raise ValueError()
        set_session_credential(payload.get("key"))
    except (ValueError, TypeError):
        raise HTTPException(400, "API 키를 확인해 주세요.") from None
    return credential_status()


@router.delete("/credentials")
def remove_credentials():
    clear_session_credential()
    return credential_status()


@router.post("/profiles/{profile}/open")
def open_login_profile(profile: str):
    if profile not in PROFILES:
        raise HTTPException(400, "유튜브 프로필을 확인해 주세요.")
    try:
        open_profile(profile)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(503, "로그인 창을 열지 못했습니다. Chrome 설치를 확인해 주세요.") from exc
    return {"profile": profile, "message": "열린 크롬에서 로그인한 뒤 창을 닫아 주세요."}


def _run_rising_job(job_id: str) -> None:
    def progress(**values):
        with _job_lock:
            _rising_jobs[job_id].update(values)
    try:
        result = find_rising(store, progress)
        with _file_lock:
            _write_json(RISING_FILE, {"result": result, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        progress(stage="complete", result=result, error=None)
    except Exception as exc:
        progress(stage="failed", error=str(exc)[:220] or "영상 탐색을 완료하지 못했습니다.")


@router.post("/rising/jobs")
def start_rising_job():
    with _job_lock:
        active = next((job for job in _rising_jobs.values() if job["stage"] not in ("complete", "failed")), None)
        if active:
            return {"job_id": active["id"]}
        job_id = uuid4().hex
        _rising_jobs[job_id] = {"id": job_id, "stage": "queued", "result": None, "error": None}
    try:
        _executor.submit(_run_rising_job, job_id)
    except RuntimeError as exc:
        with _job_lock:
            _rising_jobs[job_id].update(stage="failed", error="영상 탐색을 시작하지 못했습니다.")
        raise HTTPException(503, "영상 탐색을 시작하지 못했습니다.") from exc
    return {"job_id": job_id}


@router.get("/rising/active")
def active_rising_job():
    with _job_lock:
        active = next((job for job in _rising_jobs.values() if job["stage"] not in ("complete", "failed")), None)
        return deepcopy(active) if active else {"id": None}


@router.get("/rising/latest")
def latest_rising_job():
    with _file_lock:
        try:
            saved = json.loads(RISING_FILE.read_text(encoding="utf-8"))
            result = saved.get("result") or {}
            for video in result.get("videos", []):
                video.pop("draft_only", None)
            return saved
        except (FileNotFoundError, OSError, ValueError):
            return {"result": None}


@router.get("/rising/jobs/{job_id}")
def rising_job_status(job_id: str):
    with _job_lock:
        job = deepcopy(_rising_jobs.get(job_id))
    if not job:
        raise HTTPException(404, "영상 탐색 기록을 찾지 못했습니다.")
    return job


@router.put("/settings")
def save_settings(settings: Settings):
    value = settings.model_dump()
    value["generation_models"] = {key: str(value["generation_models"].get(key, ""))[:120] for key in ("local", "openai", "gcp")}
    value["generation_models"][value["generation_provider"]] = value["generation_model"]
    value["jev_models"] = {key: str(value["jev_models"].get(key, ""))[:120] for key in ("existing", "official")}
    value["jev_models"][value["jev_provider"]] = value["jev_model"]
    with _file_lock:
        _write_json(SETTINGS_FILE, value)
    return value


@router.get("/analysis/{video_id}")
def get_analysis(video_id: str):
    try:
        video_id = normalize_video_id(video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    analysis = _load(video_id)
    if not analysis:
        raise HTTPException(404, "저장된 영상 분석이 없습니다.")
    with _job_lock:
        active_ids = _review_jobs | {job["id"] for job in _jobs.values() if job["stage"] not in ("complete", "failed")}
    interrupted = [key for key, value in analysis.get("draft_versions", {}).items()
                   if value.get("review_pending") and key not in active_ids]
    if interrupted:
        def mark(current):
            for key in interrupted:
                record = current["draft_versions"].get(key)
                if record and record.get("review_pending"):
                    record["review_pending"] = False
                    record["review"] = {"action": "review_unavailable", "reason": "검토가 중단됐어요. 다시 진단해 주세요."}
        analysis = _change_analysis(video_id, mark)
    return _public(analysis)


@router.get("/active/{video_id}")
def active_job(video_id: str):
    try:
        video_id = normalize_video_id(video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with _job_lock:
        active = next((job for job in _jobs.values() if job["video_id"] == video_id
                       and job["stage"] not in ("complete", "failed")), None)
        return {"job_id": active["id"], "stage": active["stage"]} if active else {"job_id": None}


@router.post("/jobs")
def start_job(request: StartRequest):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    job_id = uuid4().hex
    with _job_lock:
        if video_id in _busy:
            raise HTTPException(409, "이 영상을 이미 분석하고 있습니다.")
        _busy.add(video_id)
        _jobs[job_id] = {"id": job_id, "video_id": video_id, "stage": "queued", "result": None, "error": None}
    try:
        _executor.submit(_run_job, job_id, request, video_id, _settings())
    except RuntimeError as exc:
        _finish_job(job_id, video_id, stage="failed", error="분석 작업을 시작하지 못했습니다.")
        raise HTTPException(503, "분석 작업을 시작하지 못했습니다.") from exc
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    with _job_lock:
        job = deepcopy(_jobs.get(job_id))
    if not job:
        raise HTTPException(404, "분석 작업을 찾지 못했습니다.")
    analysis = _load(job["video_id"])
    if analysis:
        job["result"] = _public(analysis)
    return job


@router.post("/drafts/edit")
def edit_draft(request: DraftEditRequest):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    def edit(current):
        record = current["draft_versions"].get(request.draft_id)
        if not record:
            raise HTTPException(404, "수정할 시안을 찾지 못했습니다.")
        if record.get("edit_revision", 0) != request.expected_revision:
            raise HTTPException(409, "다른 곳에서 시안이 변경됐어요. 저장된 내용을 확인해 주세요.")
        if record.get("comment", "") != request.comment:
            record["comment"] = request.comment
            record["edit_revision"] = record.get("edit_revision", 0) + 1
            record["character_count"] = character_count(request.comment)
    analysis = _change_analysis(video_id, edit)
    return {"draft_id": request.draft_id, "edit_revision": analysis["draft_versions"][request.draft_id].get("edit_revision", 0),
            "analysis_revision": analysis.get("analysis_revision", 0)}


@router.post("/drafts/select")
def select_draft(request: DraftSelectRequest):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    def select(current):
        if request.draft_id not in current["draft_versions"]:
            raise HTTPException(404, "선택한 시안을 찾지 못했습니다.")
        if request.expected_active_id is not None and current.get("active_draft_id") != request.expected_active_id:
            raise HTTPException(409, "다른 곳에서 선택한 시안이 바뀌었어요. 다시 확인해 주세요.")
        current["active_draft_id"] = request.draft_id
        if current.get("candidate_draft_id") == request.draft_id:
            current["candidate_draft_id"] = None
    return _public(_change_analysis(video_id, select))


@router.post("/drafts/correction")
def accept_correction(request: CorrectionRequest):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    def accept(current):
        source = current["draft_versions"].get(request.draft_id)
        if not source or current.get("active_draft_id") != request.draft_id:
            raise HTTPException(409, "현재 선택된 시안을 다시 확인해 주세요.")
        proposal = source.get("correction_suggestion") or {}
        if source.get("edit_revision", 0) != request.expected_revision or source.get("comment") != proposal.get("source_text"):
            raise HTTPException(409, "교정 기준과 현재 초안이 달라요. 현재 댓글을 다시 진단해 주세요.")
        comment = proposal.get("comment", "")
        if not comment or validate_integrated(comment):
            raise HTTPException(400, "교정안의 형식을 확인해 주세요.")
        version = deepcopy(source)
        new_id = uuid4().hex
        version.update({"id": new_id, "comment": comment, "source_text": comment,
            "original": source.get("comment", ""), "diagnosis": proposal.get("diagnosis"),
            "diagnosis_text": comment if proposal.get("diagnosis") else None,
            "review": proposal.get("review"), "mentions_evidence": proposal.get("mentions_evidence"),
            "character_count": character_count(comment), "corrected": True, "review_pending": False,
            "edit_revision": 0, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        version.pop("correction_suggestion", None)
        current["draft_versions"][new_id] = version
        current["active_draft_id"] = new_id
        current["candidate_draft_id"] = None
    return _public(_change_analysis(video_id, accept))


def _run_manual_correction(video_id: str, draft_id: str, source: str, case: dict,
                           selected: list[str], previous: list[str], settings: dict) -> None:
    proposal, message = None, ""
    try:
        def correcting(current):
            record = current["draft_versions"].get(draft_id)
            if record:
                record["review_stage"] = "correcting"
        _change_analysis(video_id, correcting)
        result = generate_json(settings, compile_feedback(selected), {**case, "comment": source, "other_comments": previous[-4:]})
        answer = result.get("answer", {})
        revised = str(answer.get("comment", "")).strip()
        if answer.get("decision") == "rewrite" and revised and not validate_integrated(revised):
            def verifying(current):
                record = current["draft_versions"].get(draft_id)
                if record:
                    record["review_stage"] = "verifying"
            _change_analysis(video_id, verifying)
            diagnostic = diagnose(settings, case, revised, previous[-4:])
            review = route_signals(diagnostic["scores"])
            if review["action"] == "rewrite":
                review["reason"] = "교정 후에도 높은 검토 신호가 남아 있어 직접 확인해 주세요."
            proposal = {"comment": revised, "source_text": source, "diagnosis": diagnostic,
                "review": review, "mentions_evidence": comment_mentions_evidence(revised, case.get("scene_quote", "")),
                "model": result.get("model")}
        elif answer.get("decision") == "keep":
            message = "현재 초안을 유지하는 것으로 검토됐어요."
        elif answer.get("decision") == "hold":
            message = str(answer.get("reason") or "교정안을 보류했습니다.")[:300]
        else:
            message = "교정안의 형식을 확인하지 못해 현재 초안을 유지했어요."
    except (ProviderError, ValueError, TypeError) as exc:
        message = str(exc)[:300] or "교정 요청을 완료하지 못했습니다."
    except Exception:
        message = "교정 요청을 완료하지 못했습니다. 다시 시도해 주세요."
    try:
        def finish(current):
            record = current["draft_versions"].get(draft_id)
            if not record:
                return
            record["review_pending"] = False
            record["review_stage"] = "complete" if not message else "failed"
            record.pop("correction_error", None)
            if proposal:
                record["correction_suggestion"] = proposal
            if message:
                record["correction_error"] = message
        _change_analysis(video_id, finish)
    finally:
        with _job_lock:
            _review_jobs.discard(draft_id)


@router.post("/drafts/correct")
def request_correction(request: CorrectionRequest):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    analysis = _load(video_id)
    record = (analysis or {}).get("draft_versions", {}).get(request.draft_id)
    if not record or analysis.get("active_draft_id") != request.draft_id:
        raise HTTPException(409, "현재 선택된 시안을 다시 확인해 주세요.")
    if not record.get("grounded"):
        raise HTTPException(400, "영상 장면을 먼저 확인해 주세요.")
    if record.get("edit_revision", 0) != request.expected_revision or record.get("diagnosis_text") != record.get("comment", "").strip():
        raise HTTPException(409, "현재 댓글을 먼저 다시 진단해 주세요.")
    selected = (record.get("review") or {}).get("selected") or []
    if not selected or record.get("review_pending"):
        raise HTTPException(400, "교정할 진단 신호를 확인해 주세요.")
    with _job_lock:
        if request.draft_id in _review_jobs:
            raise HTTPException(409, "이 시안의 검토가 진행 중입니다.")
        _review_jobs.add(request.draft_id)
    try:
        def start(current):
            value = current["draft_versions"].get(request.draft_id)
            if (not value or value.get("edit_revision", 0) != request.expected_revision
                    or current.get("active_draft_id") != request.draft_id
                    or value.get("diagnosis_text") != record.get("diagnosis_text")
                    or (value.get("review") or {}).get("selected") != selected):
                raise HTTPException(409, "시안이 바뀌었어요. 다시 진단해 주세요.")
            value["review_pending"] = True
            value["review_stage"] = "correction_queued"
            value.pop("correction_error", None)
        updated = _change_analysis(video_id, start)
        _review_executor.submit(_run_manual_correction, video_id, request.draft_id, record["comment"],
                         deepcopy(record["case"]), list(selected), _recent_comments(video_id, analysis), _settings())
        return _public(updated)
    except Exception:
        with _job_lock:
            _review_jobs.discard(request.draft_id)
        try:
            def failed(current):
                value = current["draft_versions"].get(request.draft_id)
                if value:
                    value["review_pending"] = False
                    value["review_stage"] = "failed"
                    value["correction_error"] = "교정 작업을 시작하지 못했습니다."
            _change_analysis(video_id, failed)
        except HTTPException:
            pass
        raise


@router.post("/drafts/variant")
def preview_variant(request: VariantRequest):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    analysis = _load(video_id)
    record = (analysis or {}).get("draft_versions", {}).get(request.draft_id)
    if not record:
        raise HTTPException(404, "시안을 찾지 못했습니다.")
    scene = next((x for x in analysis.get("scenes", []) if x["id"] == record.get("scene_id")), {})
    result = apply_variant(request.comment, evidence_quote=scene.get("quote", ""), case_id=request.draft_id, rate=1)
    if result.get("applied") and validate_integrated(result["comment"]):
        return {"comment": request.comment, "applied": False, "reason": "invalid_result"}
    return result


@router.post("/evidence")
def add_evidence(request: EvidenceRequest):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    added = {}
    def add(analysis):
        try:
            scene = manual_scene(analysis["context"], request.quote)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        scene["id"] = "manual-" + uuid4().hex[:12]
        analysis["context"].setdefault("manual_evidence", []).append(scene["quote"])
        analysis["scenes"].append(scene)
        added["id"] = scene["id"]
    result = _public(_change_analysis(video_id, add))
    result["added_scene_id"] = added["id"]
    return result


@router.post("/review")
def review_draft(request: ReviewRequest):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    analysis = _load(video_id)
    draft_id = request.draft_id or (analysis or {}).get("active_draft_id")
    draft = (analysis or {}).get("draft_versions", {}).get(draft_id) or {}
    if not draft.get("case"):
        raise HTTPException(400, "먼저 댓글을 생성해 주세요.")
    if draft.get("review_pending"):
        raise HTTPException(409, "이 시안의 검토가 진행 중입니다.")
    comment = request.comment.strip()
    errors = validate_integrated(comment)
    if errors:
        raise HTTPException(400, " ".join(errors))
    scene = next((x for x in analysis["scenes"] if x["id"] == draft.get("scene_id")), None)
    if not scene or not scene.get("quote"):
        raise HTTPException(400, "먼저 영상 장면을 확인해 주세요.")
    revision = draft.get("edit_revision", 0)
    claim = (video_id, draft_id)
    with _job_lock:
        if claim in _reviews:
            raise HTTPException(409, "이 시안을 이미 진단하고 있습니다.")
        _reviews.add(claim)
    try:
        settings = _settings()
        try:
            diagnostic = diagnose(settings, draft["case"], comment, _recent_comments(video_id, analysis))
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc
        from .natural_comment import route_signals
        review = route_signals(diagnostic["scores"])
        if review["action"] == "rewrite":
            review["reason"] = "높은 검토 신호가 있어 댓글을 직접 확인해 주세요."
        def save_review(current):
            record = current["draft_versions"].get(draft_id)
            if not record:
                raise HTTPException(404, "진단할 시안을 찾지 못했습니다.")
            if record.get("edit_revision", 0) == revision and record.get("comment") != comment:
                current["previous_drafts"] = (current.get("previous_drafts", []) + [record.get("comment", "")])[-10:]
                record["comment"] = comment
                record["edit_revision"] = revision + 1
                record["character_count"] = character_count(comment)
                record["corrected"] = False
            record.update({"diagnosis": diagnostic, "diagnosis_text": comment, "review": review,
                           "mentions_evidence": comment_mentions_evidence(comment, scene["quote"])})
            record.setdefault("operations", []).append({"step": "manual_review", "provider": settings["jev_provider"],
                "model": diagnostic.get("model"), "usage": diagnostic.get("usage", {}),
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return _public(_change_analysis(video_id, save_review))
    finally:
        with _job_lock:
            _reviews.discard(claim)


def _run_post(attempt_id: int) -> None:
    try:
        post_comment(store, attempt_id, headless=False)
    except Exception:
        # The posting worker records the failure and evidence in the existing store.
        pass


@router.post("/post")
def post(request: PostRequest, background: BackgroundTasks):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with _claim_video(video_id):
        return _reserve_post(request, background, video_id)


def _reserve_post(request: PostRequest, background: BackgroundTasks, video_id: str):
    analysis = _load(video_id)
    if not analysis or not analysis.get("draft"):
        raise HTTPException(400, "먼저 해당 영상의 댓글을 생성해 주세요.")
    draft = analysis["draft"]
    if request.draft_id is not None and request.draft_id != analysis.get("active_draft_id"):
        raise HTTPException(409, "선택한 시안이 변경됐어요. 댓글과 장면을 다시 확인해 주세요.")
    if request.expected_revision is not None and request.expected_revision != draft.get("edit_revision", 0):
        raise HTTPException(409, "댓글이 변경됐어요. 다시 확인해 주세요.")
    if draft.get("review_pending"):
        raise HTTPException(409, "현재 시안의 Jev 검토가 진행 중입니다.")
    scene = next((x for x in analysis.get("scenes", []) if x["id"] == analysis.get("selected_scene")), None)
    if not scene or not scene.get("quote") or not (scene.get("manual") or evidence_is_grounded(scene["quote"], analysis["context"])):
        raise HTTPException(400, "영상의 구체적인 장면을 확인해 주세요.")
    comment = request.comment.strip()
    errors = validate_integrated(comment)
    if errors:
        raise HTTPException(400, " ".join(errors))
    if draft.get("action") != "draft":
        raise HTTPException(400, "보류된 시안은 다시 생성하거나 검토해 주세요.")
    stale = comment != (draft.get("diagnosis_text") or draft.get("comment", ""))
    unclear = not comment_mentions_evidence(comment, scene["quote"])
    review = draft.get("review", {}).get("action", "review_unavailable")
    if (stale or unclear or review in ("review_pending", "review_facts", "review_constraints", "review_unavailable", "review_signal", "rewrite", "hold")) and not request.review_acknowledged:
        raise HTTPException(400, "영상 장면과 최종 댓글을 직접 확인해 주세요.")
    try:
        attempt_id = store.reserve(video_id, request.profile, comment)
    except LimitError as exc:
        raise HTTPException(409, str(exc)) from exc
    background.add_task(_run_post, attempt_id)
    return {"attempt_id": attempt_id, "status": "reserved", "profile": store.get(attempt_id)["profile"],
            "delete_after_confirmation": True}


@router.get("/status")
def status():
    return store.snapshot()


@router.get("/capture/{attempt_id}")
def capture(attempt_id: int):
    attempt = store.get(attempt_id)
    if not attempt:
        raise HTTPException(404, "작업 기록을 찾지 못했습니다.")
    candidates = [event.get("screenshot") for event in store.snapshot()["events"] if event.get("attempt_id") == attempt_id]
    if attempt["status"] == "confirmed":
        candidates.insert(0, attempt.get("screenshot"))
    else:
        candidates.append(attempt.get("screenshot"))
    root = (OUTPUT_DIR / "poc-evidence" / "screenshots").resolve()
    for item in candidates:
        if not item:
            continue
        path = Path(item).resolve()
        if path.is_relative_to(root) and path.suffix.lower() == ".png" and path.is_file():
            return FileResponse(path, media_type="image/png")
    raise HTTPException(404, "저장된 확인 화면이 없습니다.")
