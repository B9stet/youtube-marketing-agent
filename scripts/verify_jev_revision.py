"""Experiment with Jev signals for meaning preservation after a rewrite."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poc.jev_humanizer import route_revision, verify_revision
from scripts.run_jev_humanizer import collect_cases, json_read, json_write, outcome_error, prepare_existing_jev, replace_report_section, require_same_cases

WORK = ROOT / ".agent" / "jev-humanizer-poc"
REPORT = ROOT / "output" / "jev-humanizer-poc-2026-11-24.md"
MARKER = "\n## 수정 후 의미 보존 신호 시험\n"
LABELS = {"new_emotion": "새 감정 추가", "new_fact": "새 사실/경험 추가",
          "stance_changed": "입장/감정 방향 변경", "repetition_remains": "반복 잔존"}
ROUTES = {"review_constraints": "글자 수 등 조건 검토", "review_changed": "의미 변경 의심, 검토",
          "review_repetition": "반복 잔존 의심, 검토", "review_uncertain": "판단 애매, 검토",
          "no_issue_detected": "설정된 신호에서 문제 미발견"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path)
    parser.add_argument("--pace-seconds", type=float, default=8.0)
    args = parser.parse_args()
    if args.pace_seconds < 0 or args.pace_seconds > 60:
        raise ValueError("Jev 요청 간격은 0~60초여야 합니다.")
    if args.run:
        run = args.run.resolve()
    else:
        paths = sorted(p for p in WORK.iterdir() if p.is_dir() and (p / "results.json").is_file())
        if not paths:
            raise RuntimeError("저장된 PoC 실행 결과가 없습니다.")
        run = paths[-1].resolve()
    if not run.is_relative_to(WORK.resolve()) or not (run / "results.json").is_file():
        raise ValueError("기존 PoC 실행 폴더를 지정해야 합니다.")
    prepare_existing_jev()
    loaded_cases = collect_cases()
    require_same_cases(run, loaded_cases)
    cases = {c["case_id"]: c for c in loaded_cases}
    edits = [(path.stem, json_read(path)) for path in sorted((run / "compiler-v2").glob("*.json"))]
    edits = [(case_id, value) for case_id, value in edits if case_id in cases and value.get("comment")]
    results = {}
    with httpx.Client() as client:
        for index, (case_id, edit) in enumerate(edits, 1):
            path = run / "compiler-v2-verify" / f"{case_id}.json"
            if path.is_file() and "error" not in (saved := json_read(path)):
                results[case_id] = saved
                continue
            try:
                results[case_id] = verify_revision(cases[case_id]["comment"], edit["comment"], client=client)
            except Exception as exc:
                results[case_id] = {"error": outcome_error(exc)}
            json_write(path, results[case_id])
            print(f"수정 후 신호 {index}/{len(edits)}: {case_id} {results[case_id].get('error', '완료')}", flush=True)
            if results[case_id].get("error") == "HTTP 429":
                break
            if index < len(edits) and args.pace_seconds:
                time.sleep(args.pace_seconds)
    tokens = sum(int((value.get("usage") or {}).get("input_tokens") or 0) for value in results.values())
    lines = ["", "## 수정 후 의미 보존 신호 시험", "",
             "개선된 지시로 수정한 문장의 원문과 결과를 함께 보내 새 감정, 새 사실, 입장 변경, 반복 잔존 여부를 물었다. Jev 점수는 검토를 돕는 신호이며 실제 의미 보존을 보장하지 않는다.", "",
             f"- Jev 응답 {sum('error' not in x for x in results.values())}/{len(edits)}건. 입력 토큰 {tokens:,}개. 기존 진단 토큰과 별도다.", ""]
    for case_id, edit in edits:
        result = results.get(case_id, {"error": "not_started"})
        lines += [f"### {case_id}", "", f"- 원문: {cases[case_id]['comment']}", f"- 수정: {edit['comment']}"]
        if "error" in result:
            lines.append(f"- 검사 오류: {result['error']}")
        else:
            lines.append("- Jev 신호: " + ", ".join(f"{LABELS[k]} {v:.2f}" for k, v in result["scores"].items()))
            lines.append(f"- 임시 기준 분기: {ROUTES[route_revision(edit.get('errors', []), result['scores'])]}")
        lines.append("")
    content = REPORT.read_text(encoding="utf-8")
    REPORT.write_text(replace_report_section(content, MARKER, "\n".join(lines)), encoding="utf-8")
    return 0 if len(results) == len(edits) and all("error" not in r for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
