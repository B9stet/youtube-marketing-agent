"""Retry failed correction responses without regenerating drafts or Jev scores."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poc.jev_humanizer import rewrite, route_revision, verify_revision
from scripts.run_jev_humanizer import json_read, json_write, outcome_error, prepare_existing_jev
from scripts.run_unseen_video_poc import WORK, REPORT, build_cases, finalize_case, paced, render


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--pace-seconds", type=float, default=8.0)
    parser.add_argument("--casual-rate", type=float, default=0.30)
    args = parser.parse_args()
    if not 0 <= args.pace_seconds <= 60 or not 0 <= args.casual_rate <= 1:
        raise ValueError("요청 간격 또는 표기 변형 비율이 올바르지 않습니다.")
    run = args.run.resolve()
    if not run.is_relative_to(WORK.resolve()) or not (run / "results.json").is_file():
        raise ValueError("기존 새 영상 PoC 실행 폴더가 필요합니다.")
    cases = build_cases()
    if json_read(run / "cases.json") != cases:
        raise ValueError("원본 영상 자료가 달라져 기존 진단을 재사용할 수 없습니다.")
    records = json_read(run / "results.json")
    failed = [case for case in cases if records[case["case_id"]].get("rewrite", {}).get("error")]
    prepare_existing_jev()
    last_jev = [0.0]
    with httpx.Client() as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            for case in failed:
                record = records[case["case_id"]]
                record.setdefault("rewrite_failure_history", []).append(record["rewrite"])
                material = {"comment": record["generation"]["comment"], "evidence_quote": case["quote"], "scene": case["scene"]}
                diagnosis = {"action": "rewrite", "selected": record["correction_plan"]["selected"]}
                futures[pool.submit(rewrite, material, diagnosis, client=client)] = case
            for index, future in enumerate(as_completed(futures), 1):
                case = futures[future]
                try:
                    value = future.result()
                except Exception as exc:
                    value = {"error": outcome_error(exc)}
                records[case["case_id"]]["rewrite"] = value
                json_write(run / "rewrite-retry" / f"{case['case_id']}.json", value)
                print(f"교정 재시도 {index}/{len(failed)}: {case['case_id']} {value.get('error', '완료')}", flush=True)
        changed = [case for case in failed if records[case["case_id"]].get("rewrite", {}).get("changed")]
        for index, case in enumerate(changed, 1):
            record = records[case["case_id"]]
            try:
                value = paced(lambda: verify_revision(record["generation"]["comment"], record["rewrite"]["comment"], client=client), last_jev, args.pace_seconds)
            except Exception as exc:
                value = {"error": outcome_error(exc)}
            record["verification"] = value
            if "scores" in value:
                record["revision_route"] = route_revision(record["rewrite"].get("errors", []), value["scores"])
            json_write(run / "verification-retry" / f"{case['case_id']}.json", value)
            print(f"수정 보존 검사 {index}/{len(changed)}: {case['case_id']} {value.get('error', '완료')}", flush=True)
            if value.get("error") == "HTTP 429":
                break
    for case in cases:
        finalize_case(case, records[case["case_id"]], args.casual_rate)
    json_write(run / "results.json", records)
    started = datetime.strptime(run.name, "%Y%m%d-%H%M%S").astimezone().isoformat(timespec="seconds")
    REPORT.write_text(render(cases, records, started), encoding="utf-8")
    print(f"갱신된 보고서: {REPORT}", flush=True)
    return 0 if all(not records[c["case_id"]].get("rewrite", {}).get("error") for c in failed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
