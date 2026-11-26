"""Regenerate one new-video case after correcting its source context."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_contextual_fresh_videos import produce, review
from scripts.run_new_video_comment_poc import build_cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video_id")
    args = parser.parse_args()
    run = sorted((ROOT / ".agent/fresh-video-poc").glob("run-*"))[-1]
    case = next(case for case in build_cases(run) if case["id"] == args.video_id)
    groups = json.loads((ROOT / "poc/data/comment_tones.json").read_text(encoding="utf-8"))["groups"]
    tone = next(group for group in groups if group["id"] == case["tone_id"])
    record = produce(case, tone)
    (run / f"fresh-generation-{args.video_id}.json").write_text(json.dumps([record], ensure_ascii=False, indent=2), encoding="utf-8")
    print("재생성", args.video_id, "draft" if record.get("initial") else "hold", flush=True)
    review(args.video_id, target_run=run)


if __name__ == "__main__":
    main()
