"""Reassess saved experiment outcomes with the current non-network guard rules."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.contextual_brand_poc import confirmed_source_errors, plan_correction, post_review_status


def main() -> None:
    run = sorted((ROOT / ".agent/contextual-bridge-poc").glob("run-*"))[-1]
    rows = json.loads((run / "rerouted-results.json").read_text(encoding="utf-8"))
    assessed = []
    for row in rows:
        outcome = row.get("outcome", {})
        status = outcome.get("status", "hold")
        post = row.get("post_diagnosis", {}).get("scores")
        changed = bool(row.get("rewritten_case")) or row["case"].get("original_body", row["case"].get("body")) != row["case"].get("body")
        recomputed = None
        if post:
            recomputed = plan_correction(post, [], body_changed=changed)
            status = post_review_status(recomputed, body_changed=changed, scores=post)
        elif row.get("correction", {}).get("answer", {}).get("decision") == "keep" and row.get("diagnosis", {}).get("scores", {}).get("stance", 0) >= .75:
            status = "hold"
        assessed.append({"id": row["case"]["id"], "kind": row["kind"], "previous_status": outcome.get("status"),
                         "status": status, "post_plan": recomputed,
                         "interest_gap_checked": "interest_gap" in (post or {}),
                         "candidate": outcome.get("comment", "")})
        print(row["case"]["id"], outcome.get("status"), "→", status, flush=True)
    (run / "assessed-results.json").write_text(json.dumps(assessed, ensure_ascii=False, indent=2), encoding="utf-8")
    source_path = run / "source-review.json"
    source_reviews = json.loads(source_path.read_text(encoding="utf-8")) if source_path.exists() else []
    for path in (run / "fresh-reviewed.json", run / "fresh-reviewed-wait_in_cold.json"):
        if not path.exists():
            continue
        fresh = json.loads(path.read_text(encoding="utf-8"))
        results = []
        for row in fresh:
            case = row["case"]
            initial = row.get("initial", {})
            problems = confirmed_source_errors(source_reviews, case["id"], initial.get("comment", ""))
            status = row.get("outcome", {}).get("status", "hold")
            post = row.get("post_diagnosis", {}).get("scores")
            changed = bool(row.get("rewritten_case")) or case.get("original_body", case.get("body")) != case.get("body")
            post_plan = None
            if problems:
                status = "hold"
            elif post:
                post_plan = plan_correction(post, [], body_changed=changed)
                status = post_review_status(post_plan, body_changed=changed, scores=post)
            elif row.get("correction", {}).get("answer", {}).get("decision") == "keep" and row.get("diagnosis", {}).get("scores", {}).get("stance", 0) >= .75:
                status = "hold"
            results.append({"id": case["id"], "status": status, "previous_status": row.get("outcome", {}).get("status"),
                            "source_errors": problems, "post_plan": post_plan, "candidate": row.get("outcome", {}).get("comment", "")})
            print("fresh", case["id"], "→", status, flush=True)
        (run / path.name.replace("reviewed", "assessed")).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
