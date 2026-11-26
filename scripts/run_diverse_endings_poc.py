"""Explore different functions for the five comments in the last PoC.

Only generates and reviews drafts. It never enters or posts a YouTube comment.
"""
from __future__ import annotations

import argparse
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
from poc.contextual_brand_poc import JEV_QUESTIONS, correct, diagnose, model_json, plan_correction, post_review_status
from poc.ending_diversity import analyze_endings, ending_style
from scripts.run_jev_humanizer import prepare_existing_jev

PROMPT = (ROOT / "docs/prompts/contextual-diverse-endings.md").read_text(encoding="utf-8")
ORDER = ("Cr2XlCDrxUM", "6Hq96MZqego", "8xAfwDQtDj8", "n7ho4C1aKVw", "U7PdMvogEGQ")


def roots() -> tuple[Path, Path]:
    source = sorted((ROOT / ".agent/fresh-video-poc").glob("run-*"))[-1]
    current = sorted((ROOT / ".agent/diverse-comment-poc").glob("run-*"))
    return source, current[-1] if current else ROOT / ".agent/diverse-comment-poc/missing"


def cases_from_previous(source: Path) -> list[dict]:
    original = json.loads((source / "fresh-reviewed.json").read_text(encoding="utf-8"))
    corrected = json.loads((source / "fresh-reviewed-U7PdMvogEGQ.json").read_text(encoding="utf-8"))[0]
    focused = json.loads((source / "focused-corrections-reviewed.json").read_text(encoding="utf-8"))
    by_id = {record["case"]["id"]: record for record in original}
    by_id["U7PdMvogEGQ"] = corrected
    focused_by_id = {record["case"]["id"]: record for record in focused}
    cases = []
    for identifier in ORDER:
        record = by_id[identifier]
        case = dict(record["case"])
        if identifier in focused_by_id:
            candidate = focused_by_id[identifier]["candidate"]
            previous_comment, previous_ending = candidate["comment"], candidate["ending"]
        else:
            previous_comment = record.get("outcome", {}).get("comment", record["initial"]["comment"])
            previous_ending = record["initial"]["ending"]
        suffix = " " + previous_ending
        if not previous_comment.endswith(suffix):
            raise ValueError(f"{identifier}: 본문과 마지막 문장을 분리할 수 없습니다.")
        case["body"] = previous_comment[:-len(suffix)]
        case.pop("connection_preference", None)
        case["previous_ending"] = previous_ending
        case["previous_comment"] = previous_comment
        cases.append(case)
    recent = [case["previous_ending"] for case in cases]
    for case in cases:
        case["recent_endings"] = recent
    return cases


def make_candidates(case: dict) -> dict:
    material = {key: case.get(key, "") for key in ("title", "scene", "roles", "evidence", "body", "stance", "tone", "viewer_comment_samples", "recent_endings")}
    with httpx.Client(timeout=120) as client:
        response = model_json(PROMPT, material, client, tokens=3000)
    record = {"case": case, "generation": response, "candidates": []}
    answer = response.get("answer", {})
    if answer.get("action") == "draft" and isinstance(answer.get("candidates"), list):
        for item in answer["candidates"][:3]:
            if not isinstance(item, dict):
                continue
            ending = str(item.get("ending", "")).strip()
            record["candidates"].append({"function": str(item.get("function", "")), "ending": ending,
                                         "connection": str(item.get("connection", "")),
                                         "comment": f"{case['body']} {ending}", "errors": check_result(case["body"], ending),
                                         "style": ending_style(ending)})
    return record


def generation() -> None:
    source, _ = roots()
    run = ROOT / ".agent/diverse-comment-poc" / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=False)
    cases = cases_from_previous(source)
    (run / "generation-prompt.md").write_text(PROMPT, encoding="utf-8")
    (run / "jev-questions.json").write_text(json.dumps(JEV_QUESTIONS, ensure_ascii=False, indent=2), encoding="utf-8")
    (run / "source-cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    old = analyze_endings([case["previous_ending"] for case in cases])
    (run / "previous-diversity.json").write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    print("이전 문구", json.dumps(old, ensure_ascii=False), flush=True)
    print("저장 위치", run, flush=True)
    output = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(make_candidates, case): case["id"] for case in cases}
        for future in as_completed(futures):
            try:
                record = future.result()
            except Exception as exc:
                record = {"case": {"id": futures[future]}, "error": f"{type(exc).__name__}: {str(exc)[:180]}", "candidates": []}
            output.append(record)
            (run / "candidates.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            print("생성", futures[future], len(record["candidates"]), flush=True)


def review(*, propose_severe_corrections: bool = True) -> None:
    _, run = roots()
    records = json.loads((run / "candidates.json").read_text(encoding="utf-8"))
    selections = json.loads((run / "selections.json").read_text(encoding="utf-8"))
    chosen = []
    for record in records:
        identifier = record["case"]["id"]
        selection = selections.get(identifier, {})
        index = selection.get("candidate_index")
        if index is None:
            chosen.append({"case": record["case"], "status": "hold", "reason": selection.get("reason", "연결 후보를 보류했습니다.")})
            continue
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(record["candidates"]):
            raise ValueError(f"{identifier}: 선택한 후보 번호가 올바르지 않습니다.")
        candidate = record["candidates"][index]
        chosen.append({"case": record["case"], "candidate": candidate, "selection_reason": selection.get("reason", "")})
    endings = [item["candidate"]["ending"] for item in chosen if item.get("candidate")]
    (run / "selected-diversity.json").write_text(json.dumps(analyze_endings(endings), ensure_ascii=False, indent=2), encoding="utf-8")
    prepare_existing_jev()
    last = 0.0
    available = True
    with httpx.Client(timeout=120) as client:
        for item in chosen:
            candidate, case = item.get("candidate"), item["case"]
            if not candidate or candidate["errors"] or not available:
                item["status"] = "hold"
                continue
            recent = [other["candidate"]["ending"] for other in chosen if other.get("candidate") and other["case"]["id"] != case["id"]][:4]
            delay = 8 - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
            try:
                item["diagnosis"] = diagnose(case, candidate["comment"], candidate["ending"], recent, client)
                item["plan"] = plan_correction(item["diagnosis"]["scores"], candidate["errors"], body_changed=False)
                item["status"] = post_review_status(item["plan"], scores=item["diagnosis"]["scores"])
                # Only a serious unresolved issue triggers one additional model edit.
                severe = any(item["diagnosis"]["scores"][key] >= .75 for key in ("emotion_flattened", "easy_resolution", "interest_gap", "stance"))
                if propose_severe_corrections and item["plan"]["action"] == "rewrite" and severe:
                    original = {"comment": candidate["comment"], "ending": candidate["ending"],
                                "emotional_core": "", "bridge_reason": candidate.get("connection", "")}
                    item["correction"] = correct({**case, "recent_endings": recent}, original, item["plan"], client)
                last = time.monotonic()
            except Exception as exc:
                item["status"] = "hold"
                item["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                available = False
                last = time.monotonic()
            (run / "reviewed.json").write_text(json.dumps(chosen, ensure_ascii=False, indent=2), encoding="utf-8")
            print("진단", case["id"], item.get("plan", {}).get("action", "error"), item["status"], flush=True)
    (run / "reviewed.json").write_text(json.dumps(chosen, ensure_ascii=False, indent=2), encoding="utf-8")
    print("끝", len(chosen), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("generate", "review"))
    parser.add_argument("--diagnostics-only", action="store_true")
    args = parser.parse_args()
    if args.phase == "generate":
        generation()
    else:
        review(propose_severe_corrections=not args.diagnostics_only)
