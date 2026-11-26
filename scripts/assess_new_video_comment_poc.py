"""Reconcile Jev signals and exact-draft source findings for the new-video PoC."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.contextual_brand_poc import confirmed_source_errors, plan_correction, post_review_status


def assess(record: dict, source_reviews: list[dict]) -> dict:
    case, initial = record["case"], record.get("initial", {})
    diagnosis = record.get("diagnosis", {})
    source_errors = confirmed_source_errors(source_reviews, case["id"], initial.get("comment", ""))
    result = {"id": case["id"], "title": case.get("title", ""), "url": case.get("url", ""),
              "source_errors": source_errors, "initial": initial, "recorded_outcome": record.get("outcome", {})}
    if not initial or "scores" not in diagnosis:
        result["status"] = "hold"
        result["reason"] = "생성 또는 Jev 진단이 완료되지 않았습니다."
        return result
    changed = case.get("original_body", case.get("body", "")) != case.get("body", "")
    plan = plan_correction(diagnosis["scores"], initial.get("errors"), confirmed_source_errors=source_errors, body_changed=changed)
    result["plan"] = plan
    if plan["action"] in ("review_facts", "review_constraints", "revise_body"):
        result["status"] = "hold"
        result["reason"] = plan["reason"]
        return result
    post = record.get("post_diagnosis", {}).get("scores")
    if post:
        post_plan = plan_correction(post, [], body_changed=changed)
        result["post_plan"] = post_plan
        result["status"] = post_review_status(post_plan, body_changed=changed, scores=post)
    elif record.get("correction", {}).get("answer", {}).get("decision") == "keep" and plan["action"] == "rewrite":
        result["status"] = "hold" if diagnosis["scores"]["stance"] >= .75 else "review_correction"
        result["reason"] = "Jev가 표시했지만 LLM이 원문을 유지했습니다."
    else:
        result["status"] = record.get("outcome", {}).get("status", "hold")
    return result


def main() -> None:
    run = sorted((ROOT / ".agent/fresh-video-poc").glob("run-*"))[-1]
    reviews_path = run / "source-review.json"
    source_reviews = json.loads(reviews_path.read_text(encoding="utf-8")) if reviews_path.exists() else []
    original = json.loads((run / "fresh-reviewed.json").read_text(encoding="utf-8"))
    retry_path = run / "fresh-reviewed-U7PdMvogEGQ.json"
    retry = json.loads(retry_path.read_text(encoding="utf-8")) if retry_path.exists() else []
    all_attempts = [assess(record, source_reviews) for record in (*original, *retry)]
    final_cases = [row for row in all_attempts if row["id"] != "U7PdMvogEGQ"]
    matches = [row for row in all_attempts if row["id"] == "U7PdMvogEGQ"]
    if matches:
        final_cases.append(matches[-1])
    output = {"all_attempts": all_attempts, "final_cases": final_cases}
    (run / "assessed-results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in all_attempts:
        print(item["id"], "→", item["status"], "source_errors", len(item["source_errors"]), flush=True)


if __name__ == "__main__":
    main()
