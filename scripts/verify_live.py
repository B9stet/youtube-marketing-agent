"""Exercise the local dashboard. Actual posting requires --post and an explicit GO."""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx
from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:10101")
    parser.add_argument("--video", required=True, help="YouTube video ID or URL")
    parser.add_argument("--post", action="store_true", help="After review, post and delete one real comment")
    args = parser.parse_args()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(args.base, wait_until="domcontentloaded", timeout=30000)
        page.locator("#search").click()
        page.wait_for_function("document.querySelectorAll('.video').length >= 2", timeout=120000)
        count = page.locator(".video").count()
        page.locator("#url").fill(args.video)
        page.locator("#add").click()
        page.wait_for_function(
            "!document.querySelector('#comment').disabled && document.querySelector('#comment').value.length > 0",
            timeout=180000,
        )
        preview = {
            "search_results": count,
            "video": page.locator("#selected").inner_text(),
            "concern": page.locator("#concern").inner_text(),
            "evidence": page.locator("#evidence").inner_text(),
            "comment": page.locator("#comment").input_value(),
            "length": page.locator("#length").inner_text(),
        }
        print(json.dumps(preview, ensure_ascii=False), flush=True)
        if not args.post:
            browser.close()
            return 0
        print("READY_FOR_REVIEW: type GO to post this comment and delete it after confirmation.", flush=True)
        try:
            decision = input().strip()
        except EOFError:
            decision = ""
        if decision != "GO":
            print("게시를 시작하지 않았습니다.", flush=True)
            browser.close()
            return 1
        page.locator("#post").click()
        deadline = time.monotonic() + 180
        last = None
        with httpx.Client(timeout=10, trust_env=False) as client:
            while time.monotonic() < deadline:
                snapshot = client.get(args.base + "/api/status").json()
                attempt = snapshot["attempts"][0] if snapshot["attempts"] else None
                if attempt:
                    stage = (attempt["status"], attempt["deletion_status"])
                    if stage != last:
                        print("STAGE", *stage, flush=True)
                        last = stage
                    if attempt["status"] == "confirmed" and attempt["deletion_status"] == "deleted":
                        print(json.dumps(attempt, ensure_ascii=False), flush=True)
                        browser.close()
                        return 0
                    if attempt["status"] in ("failed", "uncertain") or attempt["deletion_status"] == "failed":
                        print(json.dumps(attempt, ensure_ascii=False), flush=True)
                        browser.close()
                        return 1
                time.sleep(1)
        print("게시 결과 대기 시간이 초과되었습니다. 중복 게시 전에 이력을 확인하세요.", flush=True)
        browser.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
