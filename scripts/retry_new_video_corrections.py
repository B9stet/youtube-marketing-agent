"""Test one focused follow-up for two edits that Jev flagged but the LLM kept."""
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
from poc.brand_bridge_compiler import check_result, compile_issues
from poc.contextual_brand_poc import CORRECTION_PROMPT, diagnose, model_json, plan_correction, post_review_status
from scripts.run_jev_humanizer import prepare_existing_jev

TARGETS = {
    "n7ho4C1aKVw": "본문이 이미 대화가 잘 통한다는 말을 했다. 그 설명을 되풀이하거나 '성향 궁합을 슬쩍 본다' 같은 안내문으로 잇지 말고, 실제 칭찬 장면과 가벼운 말투를 살펴본다.",
    "6Hq96MZqego": "본문이 걱정하는 것은 헤어진 뒤 계속 연락하면 더 힘들어질지다. 막연한 인연의 흐름 대신 그 구체적인 고민이 질문이나 감정에 이어질 수 있는지 살핀다.",
}
FOLLOWUP = """이번에는 앞선 교정이 유지된 후보를 한 번 더 점검한다. 입력의 review_target은 사람이 지정한 점검 기준이다. 뜻을 바꾸지 말고 해당 문제가 실제로 있다면 마지막 문장만 개선한다. 문제가 없거나 더 좋은 연결이 없으면 keep 또는 hold를 반환한다. 선택한 고정 규칙과 원래 조건을 지킨다."""


def run_path() -> Path:
    return sorted((ROOT / ".agent/fresh-video-poc").glob("run-*"))[-1]


def rewrite_one(record: dict) -> dict:
    case, initial = record["case"], record["initial"]
    prompt = compile_issues(["abrupt", "advertorial", "repetition"], base_prompt=CORRECTION_PROMPT) + "\n\n" + FOLLOWUP
    material = {k: case.get(k, "") for k in ("scene", "roles", "evidence", "body", "stance", "tone", "connection_preference")}
    material.update({"comment": initial["comment"], "ending": initial["ending"], "review_target": TARGETS[case["id"]]})
    with httpx.Client(timeout=120) as client:
        response = model_json(prompt, material, client)
    result = {"case": case, "original": initial, "initial_diagnosis": record.get("diagnosis"),
              "recent_endings": record.get("comparison_endings", []), "review_target": TARGETS[case["id"]],
              "compiled_prompt": prompt, "correction": response}
    answer = response.get("answer", {})
    decision = answer.get("decision")
    if decision in ("rewrite", "keep"):
        ending = initial["ending"] if decision == "keep" else str(answer.get("ending", "")).strip()
        result["candidate"] = {"ending": ending, "comment": f"{case['body']} {ending}", "errors": check_result(case["body"], ending)}
    return result


def generation() -> None:
    run = run_path()
    records = json.loads((run / "fresh-reviewed.json").read_text(encoding="utf-8"))
    records = [r for r in records if r["case"]["id"] in TARGETS and r.get("initial")]
    output = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(rewrite_one, r): r["case"]["id"] for r in records}
        for future in as_completed(futures):
            try:
                item = future.result()
            except Exception as exc:
                item = {"case": {"id": futures[future]}, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
            output.append(item)
            (run / "focused-corrections.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            print("추가 교정", futures[future], item.get("correction", {}).get("answer", {}).get("decision", "error"), flush=True)


def review() -> None:
    run = run_path()
    records = json.loads((run / "focused-corrections.json").read_text(encoding="utf-8"))
    prepare_existing_jev()
    previous = time.monotonic()
    output = []
    with httpx.Client(timeout=120) as client:
        for record in records:
            case, candidate = record["case"], record.get("candidate")
            if not candidate or candidate.get("errors"):
                record["status"] = "hold"
            elif candidate["comment"] == record["original"]["comment"]:
                record["status"] = "keep"
            else:
                delay = 8 - (time.monotonic() - previous)
                if delay > 0:
                    time.sleep(delay)
                try:
                    record["post_diagnosis"] = diagnose(case, candidate["comment"], candidate["ending"], record["recent_endings"], client)
                    record["post_plan"] = plan_correction(record["post_diagnosis"]["scores"], candidate["errors"], body_changed=False)
                    record["status"] = post_review_status(record["post_plan"], scores=record["post_diagnosis"]["scores"])
                except Exception as exc:
                    record["status"] = "hold"
                    record["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                finally:
                    previous = time.monotonic()
            output.append(record)
            (run / "focused-corrections-reviewed.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            print("추가 교정 진단", case["id"], record["status"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("generate", "review"))
    args = parser.parse_args()
    if args.phase == "generate":
        generation()
    else:
        review()
