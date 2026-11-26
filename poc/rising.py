"""Find recently uploaded videos using measured changes in their real view counts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import time
from typing import Callable

from .store import Store
from .youtube import get_video, search_flat_videos


def find_rising(store: Store, progress: Callable[..., None] = lambda **_: None, *,
                clock: Callable[[], float] = time.time, wait: Callable[[float], None] = time.sleep,
                candidate_limit: int = 80) -> dict:
    """Return only videos with 1,000–5,000 views and a measured >=150/hour rate.

    The rate is extrapolated from two actual observations at least 60 seconds apart;
    it is never calculated from views divided by the video's age.
    """
    progress(stage="searching", message="최근 연애 영상을 찾고 있어요.")
    candidates, seen = [], set()
    errors = []
    for period in ("today", "week"):
        try:
            rows = search_flat_videos("연애", limit=100, period=period)
        except Exception as exc:
            errors.append(exc)
            continue
        for video in rows:
            if video["id"] not in seen:
                seen.add(video["id"])
                candidates.append(video)
    if not candidates and errors:
        raise RuntimeError("유튜브 영상 검색에 실패했습니다. 잠시 후 다시 시도해 주세요.") from errors[0]

    # Search-list view counts are sometimes unavailable or rounded, so keep a
    # margin and always fetch the detailed count before judging a candidate.
    def priority(video: dict) -> int:
        views = video.get("views")
        if isinstance(views, int) and 1000 <= views <= 5000:
            return 0
        if views is None:
            return 1
        if isinstance(views, int) and 500 <= views <= 6000:
            return 2
        return 3

    candidates = sorted((v for v in candidates if priority(v) < 3), key=priority)[:candidate_limit]
    progress(stage="checking", message="영상별 조회수를 확인하고 있어요.", scanned=0, total=len(candidates))
    observed: list[dict] = []
    existing: list[dict] = []
    now = datetime.fromtimestamp(clock(), timezone.utc)

    def recent(video: dict) -> bool:
        stamp = video.get("published_timestamp")
        if isinstance(stamp, (int, float)):
            return stamp >= (now - timedelta(days=7)).timestamp()
        published = video.get("published")
        if published:
            try:
                return datetime.fromisoformat(published).date() >= (now - timedelta(days=7)).date()
            except (TypeError, ValueError):
                pass
        return True

    def fetch_rows(rows: list[dict], stage: str):
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(get_video, row["id"]): row for row in rows}
            for index, future in enumerate(as_completed(futures), 1):
                try:
                    video = future.result()
                except Exception:
                    video = None
                message = "영상별 조회수를 확인하고 있어요." if stage == "checking" else "조회수 증가량을 다시 확인하고 있어요."
                progress(stage=stage, message=message, scanned=index, total=len(rows), remaining_seconds=None)
                if video:
                    yield video

    for video in fetch_rows(candidates, "checking"):
        views = video.get("views")
        if isinstance(views, bool) or not isinstance(views, int) or not (1000 <= views <= 5000) or not recent(video):
            continue
        measured_at = int(clock())
        measurement = store.observe_views(video["id"], views, measured_at)
        if measurement and measurement["views_per_hour"] >= 150:
            video.update(measurement=measurement)
            existing.append(video)
        else:
            observed.append({"video": video, "at": measured_at})

    measured_count = len(observed) + len(existing)
    if observed:
        # Use one common second pass; every pair has an interval of >=60 seconds.
        until = max(row["at"] for row in observed) + 60
        while clock() < until:
            remaining = max(1, int(until - clock() + 0.999))
            progress(stage="observing", message=f"조회수 변화를 확인하고 있어요. 약 {remaining}초 남았어요.",
                     candidates=len(observed), remaining_seconds=remaining)
            wait(min(5, remaining))
        for video in fetch_rows([row["video"] for row in observed], "remeasuring"):
            views = video.get("views")
            if isinstance(views, bool) or not isinstance(views, int) or not (1000 <= views <= 5000):
                continue
            measurement = store.observe_views(video["id"], views, int(clock()))
            if measurement and measurement["views_per_hour"] >= 150:
                video.update(measurement=measurement)
                existing.append(video)

    videos = sorted(existing, key=lambda row: row["measurement"]["views_per_hour"], reverse=True)
    return {"videos": videos, "searched": len(seen), "checked": len(candidates), "measured": measured_count}
