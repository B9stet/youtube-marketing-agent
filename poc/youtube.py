"""Retrieve YouTube search results, captions and popular comments with yt-dlp."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import html
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlencode, urlparse

import webvtt
from yt_dlp import YoutubeDL

from .config import CACHE_DIR

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def normalize_video_id(value: str) -> str:
    value = value.strip()
    if VIDEO_ID.fullmatch(value):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        raise ValueError("유효한 유튜브 주소 또는 영상 ID를 입력해 주세요.")
    if host in ("youtu.be", "www.youtu.be"):
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        parts = parsed.path.strip("/").split("/")
        candidate = parts[1] if len(parts) > 1 and parts[0] in ("shorts", "live", "embed") else parse_qs(parsed.query).get("v", [""])[0]
    else:
        raise ValueError("유튜브 영상 주소만 사용할 수 있습니다.")
    if not VIDEO_ID.fullmatch(candidate):
        raise ValueError("유튜브 영상 ID를 확인할 수 없습니다.")
    return candidate


def _options(**extra: object) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "js_runtimes": {"node": {}},
    }
    opts.update(extra)
    return opts


def _video_info(item: dict) -> dict:
    vid = normalize_video_id(str(item.get("id", "")))
    raw_date = str(item.get("upload_date") or "")
    published = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else None
    return {
        "id": vid,
        "title": unicodedata.normalize("NFC", item.get("title") or ""),
        "channel": item.get("channel") or item.get("uploader") or "",
        "published": published,
        "thumbnail": item.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "url": f"https://www.youtube.com/watch?v={vid}",
        "views": item.get("view_count"),
        "published_timestamp": item.get("timestamp"),
        "live_status": item.get("live_status"),
    }


def get_video(video_id: str) -> dict:
    video_id = normalize_video_id(video_id)
    with YoutubeDL(_options()) as ydl:
        item = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    return _video_info(item)


def search_flat_videos(query: str = "연애", limit: int = 100, period: str = "week") -> list[dict]:
    """Collect candidates with an upload-date filter; flat view counts can be absent."""
    query = query.strip() or "연애"
    filters = {"today": "CAESAggC", "week": "CAESAggD"}
    if period not in filters:
        raise ValueError("검색 기간을 확인해 주세요.")
    limit = max(1, min(int(limit), 150))
    url = "https://www.youtube.com/results?" + urlencode({"search_query": query, "sp": filters[period]})
    opts = _options(noplaylist=False, extract_flat="in_playlist", playlistend=limit)
    with YoutubeDL(opts) as ydl:
        response = ydl.extract_info(url, download=False)
    videos, ids = [], set()
    for item in response.get("entries") or []:
        candidate = str((item or {}).get("id") or "")
        if VIDEO_ID.fullmatch(candidate) and candidate not in ids:
            ids.add(candidate)
            videos.append(_video_info(item))
    return videos


def search_videos(query: str = "연애", limit: int = 8) -> list[dict]:
    limit = max(2, min(int(limit), 12))
    candidates = search_flat_videos(query, limit=max(24, limit * 3), period="week")
    ids = [item["id"] for item in candidates]

    def safe_get(vid: str) -> dict | None:
        try:
            return get_video(vid)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        videos = [item for item in executor.map(safe_get, ids) if item]
    return sorted(videos, key=lambda item: (item.get("published") or "", item.get("published_timestamp") or 0), reverse=True)[:limit]


def _korean_track(info: dict) -> tuple[str | None, str | None]:
    for kind in ("subtitles", "automatic_captions"):
        tracks = info.get(kind) or {}
        for name in ("ko", "ko-orig", "ko-KR"):
            if tracks.get(name):
                return name, "manual" if kind == "subtitles" else "automatic"
        for name in tracks:
            if name.lower().startswith("ko-"):
                return name, "manual" if kind == "subtitles" else "automatic"
    return None, None


def _read_vtt(path: Path) -> list[dict]:
    segments = []
    previous = ""
    for cue in webvtt.read(str(path)):
        for line in cue.text.splitlines():
            clean = unicodedata.normalize("NFC", html.unescape(line).strip())
            clean = re.sub(r"\s+", " ", clean)
            if not clean or clean == previous:
                continue
            segments.append({"start": cue.start, "text": clean})
            previous = clean
    return segments


def _get_captions(video_id: str, info: dict) -> dict:
    language, source_type = _korean_track(info)
    if not language:
        return {"state": "absent", "source": None, "segments": [], "error": None}
    caption_dir = CACHE_DIR / "captions"
    caption_dir.mkdir(parents=True, exist_ok=True)
    matches = list(caption_dir.glob(f"{video_id}.{language}.vtt"))
    try:
        if not matches:
            opts = _options(
                writesubtitles=True,
                writeautomaticsub=True,
                subtitleslangs=[language],
                subtitlesformat="vtt",
                outtmpl=str(caption_dir / "%(id)s.%(ext)s"),
            )
            with YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            matches = list(caption_dir.glob(f"{video_id}.{language}.vtt"))
        if not matches:
            raise RuntimeError("자막 파일을 가져오지 못했습니다.")
        segments = _read_vtt(matches[0])
        if not segments:
            raise RuntimeError("자막 파일에서 텍스트를 읽지 못했습니다.")
        return {"state": "available", "source": source_type, "language": language, "segments": segments, "error": None}
    except Exception as exc:
        return {"state": "error", "source": source_type, "language": language, "segments": [], "error": str(exc)[:300]}


def get_context(video_id: str) -> dict:
    video_id = normalize_video_id(video_id)
    opts = _options(
        getcomments=True,
        extractor_args={"youtube": {"max_comments": ["15", "15", "0", "0"], "comment_sort": ["top"]}},
    )
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    comments = [
        {"text": str(item.get("text") or "")[:500], "likes": item.get("like_count") or 0}
        for item in (info.get("comments") or [])
        if item.get("text")
    ][:10]
    return {
        "video": _video_info(info),
        "description": unicodedata.normalize("NFC", info.get("description") or "")[:6000],
        "captions": _get_captions(video_id, info),
        "comments": comments,
        "comment_sort": "YouTube 인기 댓글",
    }
