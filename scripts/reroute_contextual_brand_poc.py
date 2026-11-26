"""Re-run correction after deterministic routing is corrected, reusing Jev inputs."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.contextual_brand_poc import correct, diagnose, generate, plan_correction, post_review_status, rewrite_body
from poc.drafts import character_count
from poc.natural_variants import apply_variant
from scripts.run_jev_humanizer import prepare_existing_jev
from scripts.run_contextual_brand_poc import RECENT, as_initial


def main() -> None:
    run = sorted((ROOT / ".agent/contextual-bridge-poc").glob("run-*"))[-1]
    records = json.loads((run / "results.json").read_text(encoding="utf-8"))
    result = []
    output = run / "rerouted-results.json"
    prepare_existing_jev()
    last_call = time.monotonic()
    jev_available = True

    def save():
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    with httpx.Client(timeout=120) as client:
        def post_diagnose(case, comment, ending):
            nonlocal last_call
            delay = 8 - (time.monotonic() - last_call)
            if delay > 0:
                time.sleep(delay)
            try:
                return diagnose(case, comment, ending, RECENT, client)
            finally:
                last_call = time.monotonic()

        def redo_body(item, case):
            nonlocal jev_available
            item["body_rewrite"] = rewrite_body(case, client)
            body = item["body_rewrite"].get("body")
            if not body:
                return {"status": "hold", "reason": "본문 교정이 실패했습니다."}
            new_case = {**case, "original_body": case["body"], "body": body}
            item["rewritten_case"] = new_case
            item["regeneration"] = generate(new_case, client)
            gen = item["regeneration"]
            if gen.get("answer", {}).get("action") != "draft" or "error" in gen or gen.get("errors"):
                return {"status": "hold", "reason": "본문 교정 후 마지막 문장이 조건을 만족하지 못했습니다."}
            draft = as_initial(gen)
            item["regenerated_initial"] = draft
            if jev_available:
                try:
                    item["post_diagnosis"] = post_diagnose(new_case, draft["comment"], draft["ending"])
                    item["post_plan"] = plan_correction(item["post_diagnosis"]["scores"], draft["errors"], body_changed=True)
                except Exception as exc:
                    item["post_diagnosis"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                    jev_available = False
            status = post_review_status(item["post_plan"], body_changed=True, scores=item["post_diagnosis"]["scores"]) if "post_plan" in item else "hold"
            return {"status": status, "comment": draft["comment"], "reason": "본문이 변경되어 원문과 대조가 필요합니다."}

        for record in records:
            case = record.get("rewritten_case") or record["case"]
            item = {"case": case, "kind": record["kind"], "label": record.get("label", ""),
                    "initial": record.get("initial"), "diagnosis": record.get("diagnosis"), "previous_plan": record.get("plan")}
            initial = item["initial"]
            if not initial or "scores" not in (item["diagnosis"] or {}):
                item["outcome"] = {"status": "hold", "reason": "원래 실행에서 생성 또는 Jev 진단을 완료하지 못했습니다."}
                result.append(item); save(); continue
            changed = case.get("original_body", case["body"]) != case["body"]
            item["plan"] = plan_correction(item["diagnosis"]["scores"], initial.get("errors"), confirmed_source_errors=record.get("confirmed_source_errors"), body_changed=changed)
            plan = item["plan"]
            if plan["action"] == "rewrite":
                prior = record.get("correction", {})
                if prior.get("selected") == plan["selected"] and "answer" in prior:
                    item["correction"] = prior
                    item["reused_previous_correction"] = True
                else:
                    item["correction"] = correct(case, initial, plan, client)
                edit = item["correction"]
                decision = edit.get("answer", {}).get("decision")
                if decision == "rewrite" and not edit.get("errors") and "error" not in edit:
                    if item.get("reused_previous_correction") and "scores" in record.get("post_diagnosis", {}):
                        item["post_diagnosis"] = record["post_diagnosis"]
                    elif jev_available:
                        try:
                            item["post_diagnosis"] = post_diagnose(case, edit["comment"], edit["ending"])
                        except Exception as exc:
                            item["post_diagnosis"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                            jev_available = False
                    if "scores" in item.get("post_diagnosis", {}):
                        item["post_plan"] = plan_correction(item["post_diagnosis"]["scores"], edit.get("errors"), body_changed=changed)
                    status = post_review_status(item["post_plan"], body_changed=changed, scores=item["post_diagnosis"]["scores"]) if "post_plan" in item else "hold"
                    item["outcome"] = {"status": status, "comment": edit["comment"], "reason": "수정된 의미와 연결을 검토해야 합니다."}
                elif decision == "keep" and not edit.get("errors") and "error" not in edit:
                    status = "hold" if item["diagnosis"]["scores"]["stance"] >= .75 else "review_correction"
                    item["outcome"] = {"status": status, "comment": initial["comment"], "reason": "Jev가 표시했지만 LLM이 원문을 유지해 직접 검토합니다."}
                elif decision == "revise_body" and "error" not in edit:
                    item["outcome"] = redo_body(item, case)
                else:
                    item["outcome"] = {"status": "hold", "reason": f"교정에서 {decision or 'error'}로 분기했습니다."}
            elif plan["action"] == "revise_body":
                item["outcome"] = redo_body(item, case)
            elif plan["action"] == "keep":
                item["outcome"] = {"status": "review_draft", "comment": initial["comment"], "reason": "Jev 탐색 신호가 낮습니다."}
            else:
                item["outcome"] = {"status": "hold", "reason": plan["reason"]}
            if changed and item["outcome"].get("comment") and item["outcome"]["status"] != "hold":
                item["outcome"]["status"] = "review_body_rewrite"
            if record["kind"] == "generated" and item["outcome"].get("comment") and item["outcome"]["status"] in ("review_draft", "review_correction"):
                variant = apply_variant(item["outcome"]["comment"], evidence_quote=case.get("evidence", ""),
                                        seed="contextual-poc-20261125", case_id=case["id"], rate=.30)
                item["variant"] = variant
                if variant.get("applied"):
                    item["outcome"]["comment"] = variant["comment"]
                item["outcome"]["characters"] = character_count(item["outcome"]["comment"])
            result.append(item)
            save()
            print(f"재분기 {case['id']}: {plan['action']} → {item['outcome']['status']}", flush=True)
    print(f"끝: {len(result)}건, {output}", flush=True)


if __name__ == "__main__":
    main()
