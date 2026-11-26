"""Try a single targeted correction for the observed lack-of-interest signal."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.contextual_brand_poc import correct, diagnose, plan_correction, post_review_status
from scripts.run_contextual_brand_poc import RECENT
from scripts.run_jev_humanizer import prepare_existing_jev


def main() -> None:
    run = sorted((ROOT / ".agent/contextual-bridge-poc").glob("run-*"))[-1]
    rows = json.loads((run / "rerouted-results.json").read_text(encoding="utf-8"))
    probe = json.loads((run / "interest-probe.json").read_text(encoding="utf-8"))
    record = next(r for r in rows if r["case"]["id"] == "birthday_criticized")
    sample = next(r for r in probe if r["id"] == "birthday_criticized")
    case = record["case"]
    comment = sample["comment"]
    ending = comment[len(case["body"]):].strip()
    original = {"comment": comment, "ending": ending, "emotional_core": "기다린 사람의 서운함과 상대의 무심함에 대한 분노", "bridge_reason": ""}
    plan = plan_correction(sample["diagnosis"]["scores"], body_changed=False)
    output = {"case": case, "original": original, "diagnosis": sample["diagnosis"], "plan": plan}
    if plan["action"] != "rewrite":
        output["status"] = "hold"
    else:
        with httpx.Client(timeout=120) as client:
            edit = correct(case, original, plan, client)
            output["correction"] = edit
            if edit.get("answer", {}).get("decision") == "rewrite" and not edit.get("errors") and "error" not in edit:
                prepare_existing_jev()
                time.sleep(8)
                try:
                    post = diagnose(case, edit["comment"], edit["ending"], RECENT, client)
                    output["post_diagnosis"] = post
                    output["post_plan"] = plan_correction(post["scores"], [], body_changed=False)
                    output["status"] = post_review_status(output["post_plan"], scores=post["scores"])
                except Exception as exc:
                    output["post_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                    output["status"] = "hold"
            else:
                output["status"] = "hold"
    (run / "interest-revision.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print("decision", output.get("correction", {}).get("answer", {}).get("decision"), "status", output["status"])
    print("comment", output.get("correction", {}).get("comment", ""))
    if "post_diagnosis" in output:
        scores = output["post_diagnosis"]["scores"]
        print("post", {k: scores[k] for k in ("interest_gap", "emotion_flattened", "easy_resolution", "stance")})


if __name__ == "__main__":
    main()
