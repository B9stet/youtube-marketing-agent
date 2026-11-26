"""Generate fresh reactions and brand closings, then diagnose and review them."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.contextual_brand_poc import CORRECTION_PROMPT, GENERATION_PROMPT, JEV_QUESTIONS, confirmed_source_errors, correct, diagnose, generate, model_json, plan_correction, post_review_status, rewrite_body
from poc.drafts import character_count
from poc.natural_variants import apply_variant
from scripts.run_jev_humanizer import prepare_existing_jev
from scripts.run_contextual_brand_poc import CASES, RECENT, as_initial

BODY_PROMPT = (ROOT / "docs/prompts/contextual-reaction-generation.md").read_text(encoding="utf-8")
TONE_IDS = {"wait_in_cold": "question_reaction", "speaking_carefully": "uncertain_worried", "past_memories": "qualified_view"}


def run_path() -> Path:
    return sorted((ROOT / ".agent/contextual-bridge-poc").glob("run-*"))[-1]


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def produce(source: dict, tone: dict) -> dict:
    case = {k: v for k, v in source.items() if k != "body"}
    if case["id"] == "wait_in_cold":
        case["stance"] = "기다린 사람이 서운해하는 상황에 대한 답답함과 질문"
    case["tone"] = {k: tone[k] for k in ("label", "register", "features", "caution")}
    record = {"case": case, "kind": "fresh_generated"}
    with httpx.Client(timeout=120) as client:
        material = {k: case.get(k, "") for k in ("title", "scene", "roles", "evidence", "stance", "tone", "viewer_comment_samples")}
        record["body_generation"] = model_json(BODY_PROMPT, material, client)
        body = str(record["body_generation"].get("answer", {}).get("body", "")).strip()
        bad = not body or not 30 <= character_count(body) <= 90 or any(x in body for x in ("운명원", "사주", "#", "https://", "http://"))
        if bad:
            record["outcome"] = {"status": "hold", "reason": "영상 반응 생성의 형식 조건을 확인하지 못했습니다."}
            return record
        case["body"] = body
        case["recent_endings"] = RECENT
        record["brand_generation"] = generate(case, client)
        gen = record["brand_generation"]
        if gen.get("answer", {}).get("action") == "revise_body" and "error" not in gen:
            record["body_rewrite"] = rewrite_body(case, client)
            rewritten = record["body_rewrite"].get("body")
            if rewritten:
                case = {**case, "original_body": body, "body": rewritten}
                record["case"] = case
                record["brand_regeneration"] = gen = generate(case, client)
        if gen.get("answer", {}).get("action") == "draft" and "error" not in gen:
            record["initial"] = as_initial(gen)
        else:
            record["outcome"] = {"status": "hold", "reason": "마지막 문장 생성이 보류됐거나 실패했습니다."}
    return record


def artifact(run: Path, name: str, case_id: str | None) -> Path:
    return run / f"fresh-{name}{'-' + case_id if case_id else ''}.json"


def generation(case_id: str | None = None) -> None:
    run = run_path()
    catalog = json.loads((ROOT / "poc/data/comment_tones.json").read_text(encoding="utf-8"))
    tones = {tone["id"]: tone for tone in catalog["groups"]}
    cases = [case for case in CASES if case["id"] in TONE_IDS and (not case_id or case["id"] == case_id)]
    if not cases:
        raise ValueError("해당 새 영상 사례를 찾지 못했습니다.")
    records = []
    (run / "fresh-reaction-prompt.md").write_text(BODY_PROMPT, encoding="utf-8")
    (run / "fresh-brand-generation-prompt.md").write_text(GENERATION_PROMPT, encoding="utf-8")
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(produce, case, tones[TONE_IDS[case["id"]]]): case["id"] for case in cases}
        for future in as_completed(tasks):
            try:
                record = future.result()
            except Exception as exc:
                record = {"case": {"id": tasks[future]}, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            records.append(record)
            save(artifact(run, "generation", case_id), records)
            print(f"새 영상 {tasks[future]}: {'draft' if record.get('initial') else 'hold'}", flush=True)


def review(case_id: str | None = None, *, target_run: Path | None = None, use_batch_context: bool = False) -> None:
    run = target_run if target_run is not None else run_path()
    records = json.loads(artifact(run, "generation", case_id).read_text(encoding="utf-8"))
    (run / f"fresh-correction-prompt{'-' + case_id if case_id else ''}.md").write_text(CORRECTION_PROMPT, encoding="utf-8")
    save(run / f"fresh-jev-questions{'-' + case_id if case_id else ''}.json", JEV_QUESTIONS)
    review_path = run / "source-review.json"
    source_reviews = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else []
    prepare_existing_jev()
    out = []
    previous = time.monotonic()
    available = True
    with httpx.Client(timeout=120) as client:
        def call_jev(case, draft, recent):
            nonlocal previous
            delay = 8 - (time.monotonic() - previous)
            if delay > 0:
                time.sleep(delay)
            try:
                return diagnose(case, draft["comment"], draft["ending"], recent, client)
            finally:
                previous = time.monotonic()

        for record in records:
            case = record["case"]
            draft = record.get("initial")
            if not draft or not available:
                record.setdefault("outcome", {"status": "hold", "reason": "진단을 실행하지 못했습니다."})
                out.append(record); save(artifact(run, "reviewed", case_id), out); continue
            try:
                recent = [r["initial"]["ending"] for r in records if r.get("initial") and r["case"].get("id") != case.get("id")][:4] if use_batch_context else RECENT
                record["diagnosis"] = call_jev(case, draft, recent)
                record["comparison_endings"] = recent
                body_changed = case.get("original_body", case["body"]) != case["body"]
                record["confirmed_source_errors"] = confirmed_source_errors(source_reviews, case["id"], draft["comment"])
                plan = plan_correction(record["diagnosis"]["scores"], draft["errors"], confirmed_source_errors=record["confirmed_source_errors"], body_changed=body_changed)
                record["plan"] = plan
                if plan["action"] == "rewrite":
                    record["correction"] = correct(case, draft, plan, client)
                    edit = record["correction"]
                    decision = edit.get("answer", {}).get("decision")
                    if decision == "rewrite" and not edit.get("errors") and "error" not in edit:
                        changed = {"comment": edit["comment"], "ending": edit["ending"]}
                        record["post_diagnosis"] = call_jev(case, changed, recent)
                        record["post_plan"] = plan_correction(record["post_diagnosis"]["scores"], [], body_changed=body_changed)
                        status = post_review_status(record["post_plan"], body_changed=body_changed, scores=record["post_diagnosis"]["scores"])
                        record["outcome"] = {"status": status, "comment": edit["comment"], "reason": "교정한 의미와 관심 연결을 직접 검토합니다."}
                    elif decision == "keep" and not edit.get("errors") and "error" not in edit:
                        status = "hold" if record["diagnosis"]["scores"]["stance"] >= .75 else "review_correction"
                        record["outcome"] = {"status": status, "comment": draft["comment"], "reason": "Jev가 표시했지만 LLM이 원문을 유지해 교정 여부를 직접 검토합니다."}
                    elif decision == "revise_body" and "error" not in edit:
                        plan = {"action": "revise_body"}
                    else:
                        record["outcome"] = {"status": "hold", "reason": f"교정 결과: {decision or 'error'}"}
                if plan["action"] == "revise_body":
                    record["body_rewrite"] = rewrite_body(case, client)
                    body = record["body_rewrite"].get("body")
                    if body:
                        changed_case = {**case, "original_body": case["body"], "body": body}
                        record["rewritten_case"] = changed_case
                        record["regeneration"] = generate(changed_case, client)
                        regen = record["regeneration"]
                        if regen.get("answer", {}).get("action") == "draft" and "error" not in regen and not regen.get("errors"):
                            changed = as_initial(regen)
                            record["post_diagnosis"] = call_jev(changed_case, changed, recent)
                            record["post_plan"] = plan_correction(record["post_diagnosis"]["scores"], changed["errors"], body_changed=True)
                            status = post_review_status(record["post_plan"], body_changed=True, scores=record["post_diagnosis"]["scores"])
                            record["outcome"] = {"status": status, "comment": changed["comment"], "reason": "본문이 바뀌어 원문과 대조합니다."}
                        else:
                            record["outcome"] = {"status": "hold", "reason": "본문 교정 뒤 연결이 보류됐습니다."}
                    else:
                        record["outcome"] = {"status": "hold", "reason": "본문 교정이 실패했습니다."}
                elif plan["action"] == "keep":
                    record["outcome"] = {"status": "review_draft", "comment": draft["comment"], "reason": "탐색 신호는 낮지만 문맥을 확인합니다."}
                elif plan["action"] not in ("rewrite", "revise_body"):
                    record["outcome"] = {"status": "hold", "reason": plan["reason"]}
                if body_changed and record["outcome"].get("comment") and record["outcome"]["status"] != "hold":
                    record["outcome"]["status"] = "review_body_rewrite"
                if record["outcome"].get("comment") and record["outcome"]["status"] in ("review_draft", "review_correction"):
                    record["variant"] = apply_variant(record["outcome"]["comment"], evidence_quote=case["evidence"], case_id=case["id"], rate=.30, seed="contextual-fresh-20261125")
                    if record["variant"].get("applied"):
                        record["outcome"]["comment"] = record["variant"]["comment"]
                    record["outcome"]["characters"] = character_count(record["outcome"]["comment"])
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                record["outcome"] = {"status": "hold", "reason": "Jev 또는 교정 단계가 실패했습니다."}
                available = False
            out.append(record)
            save(artifact(run, "reviewed", case_id), out)
            print(f"새 영상 진단 {case.get('id')}: {record.get('plan',{}).get('action','error')} → {record['outcome']['status']}", flush=True)
    print(f"끝: {len(out)}건", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("generate", "review"))
    parser.add_argument("--case", choices=tuple(TONE_IDS))
    args = parser.parse_args()
    if args.phase == "generate":
        generation(args.case)
    else:
        review(args.case)
