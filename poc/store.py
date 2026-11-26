"""Persistent, transaction-protected posting limits and event history."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Callable
from zoneinfo import ZoneInfo

from .config import DAILY_LIMIT, DB_PATH, PROFILES
from .youtube import normalize_video_id

SEOUL = ZoneInfo("Asia/Seoul")
ACTIVE = ("reserved", "opening", "typing", "submitting", "uncertain", "confirmed")
ACTIVE_SQL = ",".join(f"'{status}'" for status in ACTIVE)


class LimitError(ValueError):
    pass


class Store:
    def __init__(self, path: Path = DB_PATH, limit: int = DAILY_LIMIT, now: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self.now = now or (lambda: datetime.now(SEOUL))
        with self._connect() as db:
            db.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    status TEXT NOT NULL,
                    day TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    screenshot TEXT,
                    deletion_status TEXT NOT NULL DEFAULT 'not_requested',
                    deletion_error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_video
                    ON attempts(video_id) WHERE status IN ({ACTIVE_SQL});
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER NOT NULL,
                    at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    detail TEXT,
                    screenshot TEXT,
                    FOREIGN KEY(attempt_id) REFERENCES attempts(id)
                );
                CREATE TABLE IF NOT EXISTS profile_rotation (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    next_index INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO profile_rotation(id,next_index) VALUES(1,0);
                CREATE TABLE IF NOT EXISTS auto_profile_assignments (
                    video_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    assigned_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS view_observations (
                    video_id TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    views INTEGER NOT NULL,
                    PRIMARY KEY(video_id, observed_at)
                );
                CREATE INDEX IF NOT EXISTS view_observations_by_video ON view_observations(video_id, observed_at);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(attempts)")}
            if "deletion_status" not in columns:
                db.execute("ALTER TABLE attempts ADD COLUMN deletion_status TEXT NOT NULL DEFAULT 'not_requested'")
            if "deletion_error" not in columns:
                db.execute("ALTER TABLE attempts ADD COLUMN deletion_error TEXT")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    def _time(self) -> tuple[str, str]:
        current = self.now()
        if current.tzinfo is None:
            raise ValueError("시간대가 포함된 시각이 필요합니다.")
        local = current.astimezone(SEOUL)
        return local.date().isoformat(), local.isoformat(timespec="seconds")

    @staticmethod
    def _event(db: sqlite3.Connection, attempt_id: int, at: str, stage: str, detail: str | None = None, screenshot: str | None = None) -> None:
        db.execute("INSERT INTO events(attempt_id,at,stage,detail,screenshot) VALUES(?,?,?,?,?)", (attempt_id, at, stage, detail, screenshot))

    def reserve(self, video_id: str, profile: str, comment: str) -> int:
        video_id = normalize_video_id(video_id)
        if profile not in (*PROFILES, "auto"):
            raise ValueError("유튜브 프로필을 확인해 주세요.")
        day, at = self._time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                duplicate = db.execute(f"SELECT 1 FROM attempts WHERE video_id=? AND status IN ({ACTIVE_SQL})", (video_id,)).fetchone()
                if duplicate:
                    raise LimitError("이 영상은 이미 게시했거나 게시 확인이 필요합니다.")
                count = db.execute(f"SELECT COUNT(*) FROM attempts WHERE day=? AND status IN ({ACTIVE_SQL})", (day,)).fetchone()[0]
                if count >= self.limit:
                    raise LimitError(f"오늘 게시 한도 {self.limit}건에 도달했습니다.")
                if profile == "auto":
                    assignment = db.execute("SELECT profile FROM auto_profile_assignments WHERE video_id=?", (video_id,)).fetchone()
                    if assignment:
                        profile = assignment["profile"]
                    else:
                        index = db.execute("SELECT next_index FROM profile_rotation WHERE id=1").fetchone()[0] % len(PROFILES)
                        profile = PROFILES[index]
                        db.execute("INSERT INTO auto_profile_assignments(video_id,profile,assigned_at) VALUES(?,?,?)", (video_id, profile, at))
                        db.execute("UPDATE profile_rotation SET next_index=? WHERE id=1", ((index + 1) % len(PROFILES),))
                cur = db.execute(
                    "INSERT INTO attempts(video_id,profile,comment,status,day,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (video_id, profile, comment, "reserved", day, at, at),
                )
                attempt_id = int(cur.lastrowid)
                self._event(db, attempt_id, at, "reserved")
                db.commit()
                return attempt_id
            except Exception:
                db.rollback()
                raise

    def stage(self, attempt_id: int, status: str, detail: str | None = None) -> None:
        allowed = {"opening": {"reserved"}, "typing": {"opening"}}
        if status not in allowed:
            raise ValueError("지원하지 않는 진행 상태입니다.")
        _, at = self._time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row or row["status"] not in allowed[status]:
                db.rollback()
                raise ValueError("진행 상태가 올바르지 않습니다.")
            db.execute("UPDATE attempts SET status=?,updated_at=? WHERE id=?", (status, at, attempt_id))
            self._event(db, attempt_id, at, status, detail)
            db.commit()

    def begin_send(self, attempt_id: int) -> None:
        day, at = self._time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT status,day,video_id FROM attempts WHERE id=?", (attempt_id,)).fetchone()
                if not row or row["status"] != "typing":
                    raise ValueError("댓글 입력을 완료한 작업만 게시할 수 있습니다.")
                count = db.execute(f"SELECT COUNT(*) FROM attempts WHERE day=? AND status IN ({ACTIVE_SQL}) AND id<>?", (day, attempt_id)).fetchone()[0]
                if count >= self.limit:
                    raise LimitError(f"오늘 게시 한도 {self.limit}건에 도달했습니다.")
                db.execute("UPDATE attempts SET status='submitting',day=?,updated_at=? WHERE id=?", (day, at, attempt_id))
                self._event(db, attempt_id, at, "submitting")
                db.commit()
            except Exception:
                db.rollback()
                raise

    def confirm(self, attempt_id: int, screenshot: str | None = None) -> None:
        _, at = self._time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row or row["status"] not in ("submitting", "uncertain"):
                db.rollback()
                raise ValueError("게시 결과를 확인할 수 없는 상태입니다.")
            db.execute("UPDATE attempts SET status='confirmed',updated_at=?,screenshot=? WHERE id=?", (at, screenshot, attempt_id))
            self._event(db, attempt_id, at, "confirmed", screenshot=screenshot)
            db.commit()

    def record_deletion(self, attempt_id: int, deleted: bool, error: str | None = None, screenshot: str | None = None) -> None:
        _, at = self._time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row or row["status"] != "confirmed":
                db.rollback()
                raise ValueError("게시가 확인된 댓글만 삭제할 수 있습니다.")
            result = "deleted" if deleted else "failed"
            db.execute(
                "UPDATE attempts SET deletion_status=?,deletion_error=?,updated_at=? WHERE id=?",
                (result, error[:500] if error else None, at, attempt_id),
            )
            self._event(db, attempt_id, at, "deleted" if deleted else "deletion_failed", error, screenshot)
            db.commit()

    def record_evidence(self, attempt_id: int, label: str, path: str) -> None:
        _, at = self._time()
        with self._connect() as db:
            self._event(db, attempt_id, at, label, path)

    def fail(self, attempt_id: int, error: str, screenshot: str | None = None) -> str:
        _, at = self._time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row or row["status"] in ("failed", "confirmed"):
                db.rollback()
                raise ValueError("실패 처리할 수 없는 작업입니다.")
            status = "uncertain" if row["status"] in ("submitting", "uncertain") else "failed"
            db.execute("UPDATE attempts SET status=?,updated_at=?,error=?,screenshot=? WHERE id=?", (status, at, error[:500], screenshot, attempt_id))
            self._event(db, attempt_id, at, status, error[:500], screenshot)
            db.commit()
            return status

    def snapshot(self) -> dict:
        day, _ = self._time()
        with self._connect() as db:
            count = db.execute(f"SELECT COUNT(*) FROM attempts WHERE day=? AND status IN ({ACTIVE_SQL})", (day,)).fetchone()[0]
            attempts = [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id DESC LIMIT 100")]
            events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 200")]
            next_index = db.execute("SELECT next_index FROM profile_rotation WHERE id=1").fetchone()[0] % len(PROFILES)
            assignments = {row["video_id"]: row["profile"] for row in db.execute("SELECT video_id,profile FROM auto_profile_assignments")}
        return {"today": day, "count": count, "limit": self.limit, "attempts": attempts, "events": events,
                "next_auto_profile": PROFILES[next_index], "auto_assignments": assignments}

    def observe_views(self, video_id: str, views: int, observed_at: int, *, minimum_seconds: int = 60,
                      maximum_seconds: int = 3600) -> dict | None:
        """Persist a real observation and compare it with a sufficiently earlier one."""
        video_id = normalize_video_id(video_id)
        if isinstance(views, bool) or not isinstance(views, int) or views < 0:
            raise ValueError("조회수를 확인해 주세요.")
        if minimum_seconds < 1 or maximum_seconds < minimum_seconds:
            raise ValueError("조회수 관측 간격이 올바르지 않습니다.")
        observed_at = int(observed_at)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                previous = db.execute(
                    "SELECT observed_at,views FROM view_observations WHERE video_id=? AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",
                    (video_id, observed_at - minimum_seconds, observed_at - maximum_seconds),
                ).fetchone()
                db.execute("INSERT INTO view_observations(video_id,observed_at,views) VALUES(?,?,?) "
                           "ON CONFLICT(video_id,observed_at) DO UPDATE SET views=excluded.views", (video_id, observed_at, views))
                db.commit()
            except Exception:
                db.rollback()
                raise
        if not previous or views < previous["views"]:
            return None
        elapsed = observed_at - previous["observed_at"]
        return {"start_views": previous["views"], "end_views": views, "start_at": previous["observed_at"],
                "end_at": observed_at, "elapsed_seconds": elapsed, "views_added": views - previous["views"],
                "views_per_hour": (views - previous["views"]) * 3600 / elapsed}

    def get(self, attempt_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        return dict(row) if row else None
