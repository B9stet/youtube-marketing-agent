"""Visible Chrome comment posting using a manually authenticated profile."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from threading import Lock
import unicodedata
from urllib.parse import unquote, urlparse
from uuid import uuid4

from PIL import ImageGrab
from playwright.sync_api import sync_playwright

from .config import OUTPUT_DIR, PROFILE_DIR, PROFILES
from .store import Store

VIDEO_URL = "https://www.youtube.com/watch?v={video_id}"
_profile_locks = {profile: Lock() for profile in PROFILES}


def _profile_dir(profile: str) -> Path:
    if profile not in PROFILES:
        raise ValueError("유튜브 프로필을 확인해 주세요.")
    return PROFILE_DIR if profile == "youtube1" else PROFILE_DIR.parent / profile


def type_sequentially(locator, text: str, delay_ms: int = 90) -> None:
    """Emit sequential input events; never fill or paste the completed comment."""
    locator.press_sequentially(text, delay=delay_ms)


def _screenshot(page, attempt_id: int, label: str) -> str | None:
    folder = OUTPUT_DIR / "poc-evidence" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{attempt_id}-{label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    try:
        if page is not None:
            page.screenshot(path=str(path))
        else:
            ImageGrab.grab().save(path)
        return str(path)
    except Exception:
        return None


def _account_handle(page) -> str:
    avatar = page.locator("#avatar-btn").first
    avatar.click(timeout=15000)
    header = page.locator("ytd-active-account-header-renderer").first
    header.wait_for(state="visible", timeout=10000)
    handle = header.locator("#channel-handle").first.inner_text().strip()
    avatar.click(timeout=10000)
    header.wait_for(state="hidden", timeout=10000)
    if not handle.startswith("@"):
        raise RuntimeError("댓글에 사용할 유튜브 채널 핸들을 확인하지 못했습니다.")
    return unquote(handle).casefold()


def _find_own_thread(page, comment: str, handle: str):
    candidates = page.locator("ytd-comment-thread-renderer").filter(has=page.locator("#content-text", has_text=comment))
    matches = []
    expected = unicodedata.normalize("NFC", comment).replace("\u00a0", " ").strip()
    for index in range(candidates.count()):
        thread = candidates.nth(index)
        content = unicodedata.normalize("NFC", thread.locator("#content-text").first.inner_text()).replace("\u00a0", " ").strip()
        href = thread.locator("#author-text").first.get_attribute("href") or ""
        author = unquote(urlparse(href).path.strip("/")).casefold()
        if content == expected and author == handle:
            matches.append(thread)
    if len(matches) != 1:
        raise RuntimeError("방금 작성한 댓글과 계정이 정확히 일치하는 항목을 찾지 못했습니다.")
    marker = uuid4().hex
    matches[0].evaluate("(element, marker) => element.setAttribute('data-poc-target', marker)", marker)
    return page.locator(f"ytd-comment-thread-renderer[data-poc-target='{marker}']")


def _delete_own_comment(page, thread, comment: str) -> None:
    thread.locator("button[aria-label='작업 메뉴']").first.click(timeout=10000)
    option = page.locator("ytd-menu-service-item-renderer:visible, ytd-menu-navigation-item-renderer:visible").filter(has_text=re.compile(r"^\s*삭제\s*$")).first
    option.wait_for(state="visible", timeout=10000)
    option.click()
    page.wait_for_timeout(700)
    dialog = page.locator("yt-confirm-dialog-renderer:visible, ytd-confirm-dialog-renderer:visible").first
    if dialog.count() and dialog.is_visible():
        confirm = dialog.get_by_role("button", name="삭제", exact=True).first
        confirm.click(timeout=10000)
    thread.locator("#content-text").first.wait_for(state="hidden", timeout=25000)


def retry_deletion(store: Store, attempt_id: int, headless: bool = True) -> dict:
    job = store.get(attempt_id)
    if not job or job["profile"] not in _profile_locks:
        raise ValueError("게시 작업의 유튜브 프로필을 확인해 주세요.")
    with _profile_locks[job["profile"]]:
        return _retry_deletion_locked(store, attempt_id, headless)


def _retry_deletion_locked(store: Store, attempt_id: int, headless: bool = True) -> dict:
    """Delete an already-confirmed own comment without sending another comment."""
    job = store.get(attempt_id)
    if not job or job["status"] != "confirmed":
        raise ValueError("게시 확인된 댓글만 삭제를 다시 시도할 수 있습니다.")
    if job["deletion_status"] == "deleted":
        return {"status": "deleted", "attempt_id": attempt_id}
    page = None
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(_profile_dir(job["profile"])), channel="chrome", headless=headless,
            locale="ko-KR", args=["--lang=ko-KR", "--no-first-run"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(VIDEO_URL.format(video_id=job["video_id"]), wait_until="domcontentloaded", timeout=60000)
            handle = _account_handle(page)
            page.locator("#comments").first.scroll_into_view_if_needed(timeout=30000)
            page.locator("ytd-comment-thread-renderer #content-text").filter(has_text=job["comment"]).first.wait_for(state="visible", timeout=30000)
            thread = _find_own_thread(page, job["comment"], handle)
            _delete_own_comment(page, thread, job["comment"])
            screenshot = _screenshot(page, attempt_id, "deleted")
            store.record_deletion(attempt_id, True, screenshot=screenshot)
            return {"status": "deleted", "attempt_id": attempt_id, "screenshot": screenshot}
        except Exception as exc:
            screenshot = _screenshot(page, attempt_id, "deletion-failure")
            store.record_deletion(attempt_id, False, error=f"{type(exc).__name__}: {str(exc)[:300]}", screenshot=screenshot)
            raise
        finally:
            context.close()


def post_comment(store: Store, attempt_id: int, delay_ms: int = 90, headless: bool = False) -> dict:
    job = store.get(attempt_id)
    if not job or job["profile"] not in _profile_locks:
        raise ValueError("게시 작업의 유튜브 프로필을 확인해 주세요.")
    with _profile_locks[job["profile"]]:
        return _post_comment_locked(store, attempt_id, delay_ms, headless)


def _post_comment_locked(store: Store, attempt_id: int, delay_ms: int = 90, headless: bool = False) -> dict:
    """Post one reserved comment and verify it in the signed-in account's UI."""
    job = store.get(attempt_id)
    if not job or job["status"] != "reserved":
        raise ValueError("예약된 게시 작업을 찾지 못했습니다.")
    profile_dir = _profile_dir(job["profile"])
    profile_dir.mkdir(parents=True, exist_ok=True)
    page = None
    context = None
    shot = None
    video = None
    result = None
    try:
        store.stage(attempt_id, "opening")
        with sync_playwright() as playwright:
            try:
                video_dir = OUTPUT_DIR / "poc-evidence"
                video_dir.mkdir(parents=True, exist_ok=True)
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    channel="chrome",
                    headless=headless,
                    locale="ko-KR",
                    args=["--lang=ko-KR", "--no-first-run"],
                    viewport={"width": 1280, "height": 720},
                    record_video_dir=str(video_dir),
                    record_video_size={"width": 1280, "height": 720},
                )
                page = context.pages[0] if context.pages else context.new_page()
                video = page.video
                page.goto(VIDEO_URL.format(video_id=job["video_id"]), wait_until="domcontentloaded", timeout=60000)
                page.locator("#avatar-btn").first.wait_for(state="visible", timeout=25000)
                handle = _account_handle(page)
                page.locator("#comments").first.scroll_into_view_if_needed(timeout=30000)
                placeholder = page.locator("ytd-comment-simplebox-renderer #placeholder-area").first
                placeholder.wait_for(state="visible", timeout=30000)
                placeholder.click()
                editor = page.locator("ytd-commentbox #contenteditable-root[contenteditable='true']").first
                editor.wait_for(state="visible", timeout=15000)
                editor.click()
                store.stage(attempt_id, "typing")
                type_sequentially(editor, job["comment"], delay_ms=delay_ms)
                typed = unicodedata.normalize("NFC", editor.inner_text()).replace("\u00a0", " ").strip()
                expected = unicodedata.normalize("NFC", job["comment"]).replace("\u00a0", " ").strip()
                if typed != expected:
                    raise RuntimeError("입력된 댓글이 검토한 댓글과 다릅니다.")
                button = page.locator("ytd-commentbox #submit-button button").first
                button.wait_for(state="visible", timeout=10000)
                if not button.is_enabled():
                    raise RuntimeError("댓글 게시 버튼을 사용할 수 없습니다.")
                # Persist the uncertain boundary before clicking. A crash can never
                # silently release a potentially submitted comment for retry.
                store.begin_send(attempt_id)
                button.click(timeout=15000)
                posted = page.locator("ytd-comment-thread-renderer #content-text").filter(has_text=job["comment"]).first
                posted.wait_for(state="visible", timeout=35000)
                thread = _find_own_thread(page, job["comment"], handle)
                shot = _screenshot(page, attempt_id, "confirmed")
                store.confirm(attempt_id, screenshot=shot)
                try:
                    if not headless:
                        page.wait_for_timeout(2500)
                    _delete_own_comment(page, thread, job["comment"])
                    deletion_shot = _screenshot(page, attempt_id, "deleted")
                    store.record_deletion(attempt_id, True, screenshot=deletion_shot)
                    deletion = "deleted"
                    deletion_error = None
                except Exception as exc:
                    deletion_shot = _screenshot(page, attempt_id, "deletion-failure")
                    deletion_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                    store.record_deletion(attempt_id, False, error=deletion_error, screenshot=deletion_shot)
                    deletion = "failed"
                if not headless:
                    try:
                        page.wait_for_timeout(3500)
                    except Exception:
                        pass
                result = {"status": "confirmed", "deletion_status": deletion, "deletion_error": deletion_error, "attempt_id": attempt_id, "screenshot": shot}
                return result
            except Exception:
                shot = _screenshot(page, attempt_id, "failure")
                raise
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                if video is not None:
                    try:
                        video_path = str(video.path())
                        store.record_evidence(attempt_id, "video", video_path)
                        if result is not None:
                            result["video"] = video_path
                    except Exception:
                        pass
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:350]}"
        latest = store.get(attempt_id)
        if latest and latest["status"] == "confirmed":
            if latest["deletion_status"] != "deleted":
                store.record_deletion(attempt_id, False, error=error, screenshot=shot or _screenshot(None, attempt_id, "failure"))
            raise RuntimeError(f"게시는 확인됐으며 삭제 상태를 확인해야 합니다: {exc}") from exc
        state = store.fail(attempt_id, error, shot or _screenshot(None, attempt_id, "failure"))
        raise RuntimeError(f"{'게시 확인 필요' if state == 'uncertain' else '게시 전 실패'}: {exc}") from exc
