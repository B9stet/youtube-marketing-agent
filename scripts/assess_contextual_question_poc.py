"""Apply current review guards to a saved question-style experiment."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.contextual_brand_poc import post_review_status


def main() -> None:
    run = sorted((ROOT / ".agent/contextual-question-poc").glob("run-*"))[-1]
    data = json.loads((run / "results.json").read_text(encoding="utf-8"))
    assessments = []
    for record in data:
        status = record.get("outcome", {}).get("status", "hold")
        if record.get("kind") == "generated" and record.get("diagnosis", {}).get("scores"):
            if record.get("post_diagnosis", {}).get("scores"):
                status = post_review_status(record["post_plan"], scores=record["post_diagnosis"]["scores"])
            elif record.get("outcome", {}).get("status") != "hold":
                status = post_review_status(record["plan"], scores=record["diagnosis"]["scores"])
        assessments.append({"id": record["case"]["id"], "kind": record["kind"], "status": status,
                            "candidate": record.get("outcome", {}).get("comment", "")})
        print(record["case"]["id"], "→", status, flush=True)
    (run / "assessed-results.json").write_text(json.dumps(assessments, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
