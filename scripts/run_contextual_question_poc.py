"""Review the user's question-style suggestion alongside non-question examples."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.brand_bridge_compiler import check_result
from poc.contextual_brand_poc import CORRECTION_PROMPT, GENERATION_PROMPT, JEV_QUESTIONS, correct, diagnose, generate, plan_correction, post_review_status
from poc.drafts import character_count
from scripts.run_contextual_brand_poc import CASES, RECENT, as_initial
from scripts.run_jev_humanizer import prepare_existing_jev

RUN = ROOT / ".agent/contextual-question-poc" / datetime.now().strftime("run-%Y%m%d-%H%M%S")
USER_ENDING = "그럴 때는 운명원 사주라면 같은 이유로 또 부딪힐지 알 수 있을까요?"


def make_cases() -> list[dict]:
    result = []
    for case in CASES:
        if case["id"] not in ("reunion", "birthday", "chemistry"):
            continue
        item = {**case, "recent_endings": RECENT}
        if case["id"] == "reunion":
            item["connection_preference"] = {"form": "맥락에 맞는 열린 질문", "topic": "재회해도 같은 이유로 다시 부딪힐지",
                                             "user_example": USER_ENDING}
        result.append(item)
    return result


def controls(cases: list[dict]) -> list[dict]:
    by_id = {c["id"]: c for c in cases}
    records = []
    for case_id, ending, identifier, label in (
        ("reunion", USER_ENDING, "reunion_user_question", "사용자가 제안한 질문형 마지막 문장"),
        ("reunion", "운명원에서 사주라도 보고 마음 편하게 결정했으면 좋겠어요.", "reunion_easy_resolution", "사용자가 상황에 맞지 않는다고 지적한 문장"),
        ("chemistry", "이 정도 호흡이면 운명원에서 연애 사주 봐도 찰떡 궁합으로 나올 듯ㅋㅋ", "chemistry_nonquestion", "질문형이 아닌 가벼운 감상"),
    ):
        case = dict(by_id[case_id])
        case["id"] = identifier
        initial = {"ending": ending, "comment": f"{case['body']} {ending}", "errors": check_result(case["body"], ending)}
        records.append({"case": case, "kind": "comparison", "label": label, "initial": initial})
    return records


def save(records: list[dict]) -> None:
    (RUN / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_one(case: dict) -> dict:
    with httpx.Client(timeout=120) as client:
        return generate(case, client)


def main() -> None:
    RUN.mkdir(parents=True, exist_ok=False)
    (RUN / "generation-prompt.md").write_text(GENERATION_PROMPT, encoding="utf-8")
    (RUN / "correction-prompt.md").write_text(CORRECTION_PROMPT, encoding="utf-8")
    (RUN / "jev-questions.json").write_text(json.dumps(JEV_QUESTIONS, ensure_ascii=False, indent=2), encoding="utf-8")
    cases = make_cases()
    records = controls(cases) + [{"case": case, "kind": "generated"} for case in cases]
    save(records)
    print("저장 위치:", RUN, flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(generate_one, r["case"]): r for r in records if r["kind"] == "generated"}
        for future in as_completed(tasks):
            record = tasks[future]
            try:
                result = future.result()
                record["generation"] = result
                if result.get("answer", {}).get("action") == "draft" and "error" not in result:
                    record["initial"] = as_initial(result)
            except Exception as exc:
                record["generation"] = {"error": f"{type(exc).__name__}: {str(exc)[:180]}"}
            save(records)
            print("생성", record["case"]["id"], record["generation"].get("answer", {}).get("action", "error"), flush=True)

    prepare_existing_jev()
    last_call = 0.0
    available = True
    with httpx.Client(timeout=120) as client:
        def call_jev(case, initial):
            nonlocal last_call
            delay = 8 - (time.monotonic() - last_call)
            if delay > 0:
                time.sleep(delay)
            try:
                return diagnose(case, initial["comment"], initial["ending"], RECENT, client)
            finally:
                last_call = time.monotonic()

        for record in records:
            case, initial = record["case"], record.get("initial")
            if not initial or not available:
                record["outcome"] = {"status": "hold", "reason": "생성 또는 Jev 진단을 완료하지 못했습니다."}
                save(records)
                continue
            try:
                record["diagnosis"] = call_jev(case, initial)
                plan = plan_correction(record["diagnosis"]["scores"], initial["errors"], body_changed=False)
                record["plan"] = plan
                status = "comparison_only" if record["kind"] == "comparison" else post_review_status(plan, scores=record["diagnosis"]["scores"])
                record["outcome"] = {"status": status,
                                     "comment": initial["comment"], "characters": character_count(initial["comment"])}
                if record["kind"] == "generated" and case["id"] == "reunion" and plan["action"] == "rewrite":
                    record["correction"] = correct(case, initial, plan, client)
                    edit = record["correction"]
                    if edit.get("answer", {}).get("decision") == "rewrite" and not edit.get("errors") and "error" not in edit:
                        corrected = {"comment": edit["comment"], "ending": edit["ending"]}
                        record["post_diagnosis"] = call_jev(case, corrected)
                        record["post_plan"] = plan_correction(record["post_diagnosis"]["scores"], [], body_changed=False)
                        status = post_review_status(record["post_plan"], scores=record["post_diagnosis"]["scores"])
                        record["outcome"] = {"status": status, "comment": edit["comment"], "characters": character_count(edit["comment"])}
                    elif edit.get("answer", {}).get("decision") != "keep":
                        record["outcome"] = {"status": "hold", "reason": "교정 결과를 선택하지 못했습니다."}
                if record["kind"] == "generated" and plan["action"] in ("review_facts", "review_constraints", "revise_body"):
                    record["outcome"] = {"status": "hold", "reason": plan["reason"]}
                save(records)
                print("진단", case["id"], plan["action"], record["outcome"]["status"], flush=True)
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {str(exc)[:220]}"
                record["outcome"] = {"status": "hold", "reason": "Jev 진단 또는 교정에 실패했습니다."}
                available = False
                save(records)
                print("중단", case["id"], type(exc).__name__, flush=True)
    print("완료", len(records), RUN, flush=True)


if __name__ == "__main__":
    main()
