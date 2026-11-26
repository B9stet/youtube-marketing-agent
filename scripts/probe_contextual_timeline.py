"""Probe whether Jev detects a known reversed chronology using corrected subtitles."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.contextual_brand_poc import diagnose, plan_correction
from scripts.run_jev_humanizer import prepare_existing_jev
from scripts.run_contextual_brand_poc import CASES, RECENT


def main() -> None:
    run = sorted((ROOT / ".agent/contextual-bridge-poc").glob("run-*"))[-1]
    records = json.loads((run / "fresh-generation.json").read_text(encoding="utf-8"))
    old = next(r for r in records if r["case"]["id"] == "wait_in_cold")
    source = next(c for c in CASES if c["id"] == "wait_in_cold")
    case = {**source, "body": old["case"]["body"], "stance": old["case"]["stance"], "tone": old["case"]["tone"]}
    draft = old["initial"]
    prepare_existing_jev()
    time.sleep(8)
    with httpx.Client(timeout=120) as client:
        result = diagnose(case, draft["comment"], draft["ending"], RECENT, client)
    plan = plan_correction(result["scores"], draft["errors"], body_changed=False)
    verified = ["여성에게 먼저 두 차례 미안하다고 말한 뒤 주변 사람에게 사과했는데, 댓글은 순서를 다르게 나타냈다."]
    confirmed_plan = plan_correction(result["scores"], draft["errors"], confirmed_source_errors=verified, body_changed=False)
    output = {"case": case, "initial": draft, "diagnosis": result, "plan": plan, "confirmed_source_errors": verified,
              "confirmed_plan": confirmed_plan,
              "manual_ground_truth": "여성에게 두 차례 미안하다고 말한 뒤 주변 사람에게 사과했다. 기존 댓글은 순서를 다르게 나타냈다."}
    (run / "timeline-probe.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print("timeline_error", result["scores"]["timeline_error"], "role_error", result["scores"]["role_error"], "action", plan["action"])


if __name__ == "__main__":
    main()
