from playwright.sync_api import sync_playwright

import pytest
from pathlib import Path

import poc.browser as posting
from poc.browser import _delete_own_comment, _find_own_thread, type_sequentially
from poc.store import Store


def test_korean_input_is_sequential_not_fill():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.set_content("""
          <div id="editor" contenteditable="true"></div>
          <script>
            window.values = [];
            document.querySelector('#editor').addEventListener('input', event => {
              window.values.push(event.target.innerText);
            });
          </script>
        """)
        editor = page.locator("#editor")
        editor.click()
        type_sequentially(editor, "한글 입력", delay_ms=1)
        values = [value.replace("\u00a0", " ") for value in page.evaluate("window.values")]
        assert values == ["한", "한글", "한글 ", "한글 입", "한글 입력"]
        browser.close()


@pytest.mark.parametrize("menu_tag", ["ytd-menu-service-item-renderer", "ytd-menu-navigation-item-renderer"])
def test_deletes_only_matching_comment_from_current_account(menu_tag):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.set_content("""
          <ytd-comment-thread-renderer id="other">
            <a id="author-text" href="/@other">other</a>
            <div id="content-text">같은 댓글</div>
            <button aria-label="작업 메뉴" onclick="window.target=this.parentElement; menu.style.display='block'">메뉴</button>
          </ytd-comment-thread-renderer>
          <ytd-comment-thread-renderer id="own">
            <a id="author-text" href="/@owner">owner</a>
            <div id="content-text">같은 댓글</div>
            <button aria-label="작업 메뉴" onclick="window.target=this.parentElement; menu.style.display='block'">메뉴</button>
          </ytd-comment-thread-renderer>
          <MENU_TAG id="menu" style="display:none"
            onclick="window.target.remove(); this.style.display='none'">삭제</MENU_TAG>
        """.replace("MENU_TAG", menu_tag))
        with pytest.raises(RuntimeError):
            _find_own_thread(page, "같은 댓글", "@missing")
        target = _find_own_thread(page, "같은 댓글", "@owner")
        _delete_own_comment(page, target, "같은 댓글")
        assert page.locator("#own").count() == 0
        assert page.locator("#other").count() == 1
        browser.close()


@pytest.mark.parametrize("profile", ["youtube1", "youtube2", "youtube3"])
def test_full_post_confirmation_and_deletion_in_real_browser(tmp_path, monkeypatch, profile):
    fixture = tmp_path / "youtube.html"
    fixture.write_text("""
      <button id="avatar-btn" onclick="const h=document.querySelector('ytd-active-account-header-renderer'); h.style.display=h.style.display==='block'?'none':'block'">계정</button>
      <ytd-active-account-header-renderer style="display:none"><span id="channel-handle">@owner</span></ytd-active-account-header-renderer>
      <div id="comments">
        <ytd-comment-simplebox-renderer><button id="placeholder-area" onclick="editor.style.display='block'">댓글 추가</button></ytd-comment-simplebox-renderer>
        <ytd-commentbox>
          <div id="editor" style="display:none"><div id="contenteditable-root" contenteditable="true"></div></div>
          <div id="submit-button"><button onclick="publish()">댓글</button></div>
        </ytd-commentbox>
      </div>
      <ytd-menu-navigation-item-renderer id="menu" style="display:none"
        onclick="window.target.remove(); this.style.display='none'">삭제</ytd-menu-navigation-item-renderer>
      <script>
        function publish() {
          const thread = document.createElement('ytd-comment-thread-renderer');
          const author = document.createElement('a'); author.id = 'author-text'; author.href = '/@owner';
          const content = document.createElement('div'); content.id = 'content-text';
          content.innerText = document.querySelector('#contenteditable-root').innerText;
          const menuButton = document.createElement('button'); menuButton.setAttribute('aria-label','작업 메뉴');
          menuButton.onclick = () => { window.target = thread; menu.style.display = 'block'; };
          thread.append(author, content, menuButton); document.querySelector('#comments').append(thread);
        }
      </script>
    """, encoding="utf-8")
    monkeypatch.setattr(posting, "VIDEO_URL", fixture.as_uri())
    monkeypatch.setattr(posting, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(posting, "OUTPUT_DIR", tmp_path / "output")
    store = Store(tmp_path / "test.sqlite3")
    comment = "다시 만나면 행복할까 하는 말이 마음에 남네요. 같은 상처가 반복될까 걱정되는 마음이 너무 공감돼요."
    attempt = store.reserve("C2LVY7NZeFU", profile, comment)
    result = posting.post_comment(store, attempt, delay_ms=1, headless=True)
    assert result["status"] == "confirmed"
    assert store.get(attempt)["profile"] == profile
    assert posting._profile_dir(profile).is_dir()
    assert result["deletion_status"] == "deleted"
    assert Path(result["screenshot"]).is_file()
    assert store.get(attempt)["deletion_status"] == "deleted"
    assert store.snapshot()["count"] == 1
    video_events = [event for event in store.snapshot()["events"] if event["stage"] == "video"]
    assert len(video_events) == 1
    assert (tmp_path / "output") in Path(video_events[0]["detail"]).parents
