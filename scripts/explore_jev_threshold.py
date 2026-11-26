"""Replay saved Jev scores at an exploratory threshold; no Jev calls are made."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poc.jev_humanizer import ALL_RULES, compile_diagnosis, rewrite
from scripts.run_jev_humanizer import collect_cases, json_read, json_write, outcome_error, replace_report_section, require_same_cases

REPORT = ROOT / "output" / "jev-humanizer-poc-2026-11-24.md"
WORK = ROOT / ".agent" / "jev-humanizer-poc"
MARKER = "\n## 추가 탐색: 교정 기준 0.50\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, help="기존 PoC 실행 폴더, 기본값은 가장 최근 완료 실행")
    args = parser.parse_args()
    if args.run:
        run = args.run.resolve()
    else:
        candidates = sorted(path for path in WORK.iterdir() if path.is_dir() and (path / "results.json").is_file())
        if not candidates:
            raise RuntimeError("저장된 PoC 실행 결과가 없습니다.")
        run = candidates[-1].resolve()
    if not run.is_relative_to(WORK.resolve()) or not (run / "results.json").is_file():
        raise ValueError("기존 PoC 실행 폴더를 지정해야 합니다.")
    cases = collect_cases()
    require_same_cases(run, cases)
    original = json_read(run / "results.json")["diagnoses"]
    # This is a separately labelled sensitivity check, not a change to the
    # primary 0.75 routing policy or a calibrated recommendation.
    changed = []
    viewer_hits = []
    for case in cases:
        result = original.get(case["case_id"], {})
        if "scores" not in result:
            continue
        exploratory = compile_diagnosis(result["scores"], high=0.50, low=0.35)
        if case["kind"] == "generated" and result.get("action") == "review_uncertain" and exploratory["action"] == "rewrite":
            changed.append((case, exploratory))
        elif case["kind"] == "viewer_control" and exploratory["action"] == "rewrite":
            viewer_hits.append(case["case_id"])
    outputs = {}
    with httpx.Client() as client, ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        for case, diagnosis in changed:
            saved = run / "exploratory" / f"{case['case_id']}.json"
            if saved.is_file():
                outputs[case["case_id"]] = json_read(saved)
            else:
                futures[pool.submit(rewrite, case, diagnosis, client=client)] = (case, diagnosis)
        for future in as_completed(futures):
            case, diagnosis = futures[future]
            try:
                output = future.result()
            except Exception as exc:
                output = {"error": outcome_error(exc)}
            record = {"threshold": 0.50, "diagnosis": diagnosis, "rewrite": output}
            outputs[case["case_id"]] = record
            json_write(run / "exploratory" / f"{case['case_id']}.json", record)
            print(f"탐색 교정: {case['case_id']} {output.get('error', '완료')}", flush=True)
    tokens = sum(int((o.get("rewrite", {}).get("usage") or {}).get("input_tokens") or 0) for o in outputs.values())
    lines = ["", "## 추가 탐색: 교정 기준 0.50", "",
             "기본 0.75 기준에서 기존 초안의 자동 교정이 0건이어서, 이미 받은 Jev 점수를 기준 0.50에 다시 대입해 보류된 초안의 수정 예시를 별도로 만들었다. Jev를 다시 호출하지 않았다. 이 값은 결과를 본 뒤 정한 탐색 기준이며 검증된 운영 기준이 아니다.", "",
             f"- 기존 초안 {len(changed)}개가 탐색용 교정 대상이 됐다. 실제 시청자 댓글 {len(viewer_hits)}/5개에도 같은 기준이 적용되었을 것이다. 실제 댓글은 교정하지 않았다.",
             f"- 추가 탐색에 사용한 수정 LLM 입력 토큰: {tokens:,}개. 위 본 시험의 수정 토큰 합계와 별도다."]
    preflight = run / "preflight.json"
    if preflight.is_file():
        trial = json_read(preflight)
        lines.append(f"- 별도의 연결 확인 {trial['checks']}건에서 Jev 입력 토큰 {trial['input_tokens']:,}개가 보고됐다. 처음 요청 제한 응답에는 사용량이 반환되지 않았다.")
    lines.append("")
    for case, diagnosis in changed:
        record = outputs[case["case_id"]]
        result = record["rewrite"]
        lines += [f"### {case['title']} / {case['method']}", "", f"- 원문: {case['comment']}",
                  "- 선택한 진단: " + ", ".join(f"{ALL_RULES[k].label} {diagnosis['scores'][k]:.2f}" for k in diagnosis["selected"])]
        if "error" in result:
            lines.append(f"- 교정 오류: {result['error']}")
        else:
            lines += [f"- 교정: {result['comment']}", f"- 기계적 검사: {' / '.join(result['errors']) if result['errors'] else '통과'}"]
        lines.append("")
    content = REPORT.read_text(encoding="utf-8")
    REPORT.write_text(replace_report_section(content, MARKER, "\n".join(lines)), encoding="utf-8")
    print(f"탐색 결과: 기존 초안 {len(changed)}건, 시청자 댓글 신호 {len(viewer_hits)}건, 입력 토큰 {tokens}", flush=True)
    return 1 if any("error" in value["rewrite"] for value in outputs.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
