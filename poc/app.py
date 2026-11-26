"""Small local dashboard for the end-to-end proof of concept."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
import unicodedata

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .browser import post_comment, retry_deletion
from .config import STATE_DIR
from .drafts import comment_mentions_evidence, evidence_is_grounded, generate_draft, validate_comment
from .store import LimitError, Store
from .youtube import get_context, get_video, normalize_video_id, search_videos
from .prototype_api import router as prototype_router

app = FastAPI(title="연애 영상 댓글 PoC", docs_url=None, redoc_url=None)
app.include_router(prototype_router)
store = Store()
analyses: dict[str, dict] = {}
analyses_lock = Lock()
retry_lock = Lock()
retrying: set[int] = set()
ANALYSIS_DIR = STATE_DIR / "analyses"


def _save_analysis(video_id: str, analysis: dict) -> None:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    (ANALYSIS_DIR / f"{video_id}.json").write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")


def _find_analysis(video_id: str) -> dict | None:
    analysis = analyses.get(video_id)
    path = ANALYSIS_DIR / f"{video_id}.json"
    if analysis is None and path.is_file():
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if candidate.get("context", {}).get("video", {}).get("id") == video_id:
                analysis = candidate
                analyses[video_id] = analysis
        except (OSError, ValueError, AttributeError):
            pass
    return analysis


class AnalyzeRequest(BaseModel):
    video_id: str


class PostRequest(BaseModel):
    video_id: str
    comment: str


class EvidenceRequest(BaseModel):
    video_id: str
    quote: str


def _public_analysis(analysis: dict) -> dict:
    context = analysis["context"]
    return {
        "video": context["video"],
        "caption_state": context["captions"]["state"],
        "caption_error": context["captions"].get("error"),
        "comment_count": len(context["comments"]),
        "draft": analysis["draft"],
    }


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "prototype.html")


@app.get("/assets/prototype.js")
def prototype_script():
    return FileResponse(Path(__file__).parent / "prototype.js", media_type="application/javascript")


@app.get("/poc")
def original_poc():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/search")
def search(query: str = "연애"):
    try:
        return {"videos": search_videos(query, 8)}
    except Exception as exc:
        raise HTTPException(502, f"영상을 불러오지 못했습니다: {str(exc)[:250]}") from exc


@app.get("/api/video")
def video(url: str):
    try:
        return get_video(normalize_video_id(url))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"영상을 확인하지 못했습니다: {str(exc)[:250]}") from exc


@app.post("/api/analyze")
def analyze(request: AnalyzeRequest):
    try:
        video_id = normalize_video_id(request.video_id)
        context = get_context(video_id)
        draft = generate_draft(context)
        with analyses_lock:
            analyses[video_id] = {"context": context, "draft": draft}
            _save_analysis(video_id, analyses[video_id])
        return _public_analysis(analyses[video_id])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"영상 분석에 실패했습니다: {str(exc)[:300]}") from exc


@app.get("/api/analysis")
def cached_analysis(video_id: str):
    try:
        video_id = normalize_video_id(video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with analyses_lock:
        analysis = _find_analysis(video_id)
    if not analysis:
        raise HTTPException(404, "저장된 분석이 없습니다.")
    return _public_analysis(analysis)


@app.post("/api/evidence")
def add_evidence(request: EvidenceRequest):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    quote = unicodedata.normalize("NFC", request.quote).strip()
    if len(quote) < 4 or len(quote) > 500:
        raise HTTPException(400, "영상에서 직접 확인한 말을 4~500자로 입력해 주세요.")
    with analyses_lock:
        analysis = _find_analysis(video_id)
        if not analysis:
            raise HTTPException(400, "먼저 해당 영상을 분석해 주세요.")
        analysis["context"].setdefault("manual_evidence", []).append(quote)
        analysis["draft"]["evidence_quote"] = quote
        analysis["draft"]["grounded"] = True
        analysis["draft"]["requires_review"] = False
        analysis["draft"]["evidence_source"] = "사용자 확인"
        _save_analysis(video_id, analysis)
    return _public_analysis(analysis)


def _run_post(attempt_id: int) -> None:
    try:
        post_comment(store, attempt_id, headless=False)
    except Exception:
        # post_comment records the error and screenshot in the persistent history.
        pass


def _retry_deletion(attempt_id: int) -> None:
    try:
        retry_deletion(store, attempt_id, headless=True)
    except Exception:
        # retry_deletion records the failure for the dashboard.
        pass
    finally:
        with retry_lock:
            retrying.discard(attempt_id)


@app.post("/api/post")
def post(request: PostRequest, background: BackgroundTasks):
    try:
        video_id = normalize_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with analyses_lock:
        analysis = _find_analysis(video_id)
    if not analysis:
        raise HTTPException(400, "먼저 해당 영상을 분석해 주세요.")
    comment = unicodedata.normalize("NFC", request.comment).strip()
    errors = validate_comment(comment)
    evidence = analysis["draft"].get("evidence_quote", "")
    if not evidence_is_grounded(evidence, analysis["context"]):
        errors.append("영상 발언의 근거를 확인한 뒤 게시해 주세요.")
    elif not comment_mentions_evidence(comment, evidence):
        errors.append("댓글에 확인한 영상 발언을 반영해 주세요.")
    if errors:
        raise HTTPException(400, " ".join(errors))
    try:
        attempt_id = store.reserve(video_id, "youtube1", comment)
    except LimitError as exc:
        raise HTTPException(409, str(exc)) from exc
    background.add_task(_run_post, attempt_id)
    return {"attempt_id": attempt_id, "status": "reserved"}


@app.get("/api/status")
def status():
    result = store.snapshot()
    with retry_lock:
        result["retrying_deletions"] = list(retrying)
    return result


@app.post("/api/retry-deletion/{attempt_id}")
def retry_cleanup(attempt_id: int, background: BackgroundTasks):
    attempt = store.get(attempt_id)
    if not attempt or attempt["status"] != "confirmed" or attempt["deletion_status"] != "failed":
        raise HTTPException(400, "삭제에 실패한 게시 확인 건만 다시 시도할 수 있습니다.")
    with retry_lock:
        if attempt_id in retrying:
            raise HTTPException(409, "이미 삭제를 다시 확인하고 있습니다.")
        retrying.add(attempt_id)
    background.add_task(_retry_deletion, attempt_id)
    return {"attempt_id": attempt_id, "status": "retrying"}
