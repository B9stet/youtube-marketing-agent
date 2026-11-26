"""Compare two deterministic rule compilers using the same saved Jev signals."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poc.jev_humanizer import rewrite
from scripts.run_jev_humanizer import collect_cases, json_read, json_write, outcome_error, replace_report_section, require_same_cases

WORK = ROOT / ".agent" / "jev-humanizer-poc"
REPORT = ROOT / "output" / "jev-humanizer-poc-2026-11-24.md"
MARKER = "\n## 교정 지시 개선 비교\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path)
    args = parser.parse_args()
    if args.run:
        run = args.run.resolve()
    else:
        paths = sorted(p for p in WORK.iterdir() if p.is_dir() and (p / "results.json").is_file())
        if not paths:
            raise RuntimeError("저장된 PoC 실행 결과가 없습니다.")
        run = paths[-1].resolve()
    if not run.is_relative_to(WORK.resolve()) or not (run / "results.json").is_file():
        raise ValueError("기존 PoC 실행 폴더를 지정해야 합니다.")
    loaded_cases = collect_cases()
    require_same_cases(run, loaded_cases)
    cases = {c["case_id"]: c for c in loaded_cases}
    diagnoses = json_read(run / "results.json")["diagnoses"]
    targets = []
    for case_id, diagnosis in diagnoses.items():
        old = diagnosis.get("rewrite") or {}
        if case_id in cases and cases[case_id]["kind"] == "synthetic_control" and old and old.get("compiler_version", "1") == "1":
            targets.append((cases[case_id], diagnosis, diagnosis["rewrite"], "기본 기준 0.75"))
    for path in sorted((run / "exploratory").glob("*.json")):
        saved = json_read(path)
        old = saved.get("rewrite", {})
        if path.stem in cases and "error" not in old and old.get("compiler_version", "1") == "1":
            targets.append((cases[path.stem], saved["diagnosis"], saved["rewrite"], "탐색 기준 0.50"))
    outputs = {}
    with httpx.Client() as client, ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for case, diagnosis, _, _ in targets:
            path = run / "compiler-v2" / f"{case['case_id']}.json"
            if path.is_file():
                outputs[case["case_id"]] = json_read(path)
            else:
                futures[pool.submit(rewrite, case, diagnosis, client=client)] = case
        for future in as_completed(futures):
            case = futures[future]
            try:
                value = future.result()
            except Exception as exc:
                value = {"error": outcome_error(exc)}
            outputs[case["case_id"]] = value
            json_write(run / "compiler-v2" / f"{case['case_id']}.json", value)
            print(f"개선 지시 비교: {case['case_id']} {value.get('error', '완료')}", flush=True)
    tokens = sum(int((value.get("usage") or {}).get("input_tokens") or 0) for value in outputs.values())
    lines = ["", "## 교정 지시 개선 비교", "",
             "첫 교정에서 보고서 같은 표현과 반복이 남는 것을 보고, 같은 Jev 점수와 원문을 유지한 채 교정 규칙을 더 구체적으로 바꿔 다시 시험했다. 글자 수를 맞추기 위한 반복과 정보 추가를 금지했다. 모델의 생성 변동도 있어 한 번씩의 비교로 개선 효과를 확정하지 않는다.", "",
             f"- Jev 추가 호출 없음. 개선된 지시로 교정한 LLM 입력 토큰: {tokens:,}개.", ""]
    for case, _, earlier, threshold in targets:
        result = outputs[case["case_id"]]
        label = "직접 만든 점검 문장" if case["kind"] == "synthetic_control" else f"기존 초안 / {case['title']}"
        lines += [f"### {label} ({threshold})", "", f"- 원문: {case['comment']}",
                  f"- 최초 지시 결과: {earlier.get('comment', earlier.get('error', '없음'))}"]
        if "error" in result:
            lines.append(f"- 개선 지시 오류: {result['error']}")
        else:
            lines += [f"- 개선 지시 결과: {result['comment']}",
                      f"- 기계적 검사: {' / '.join(result['errors']) if result['errors'] else '통과'}"]
        lines.append("")
    content = REPORT.read_text(encoding="utf-8")
    REPORT.write_text(replace_report_section(content, MARKER, "\n".join(lines)), encoding="utf-8")
    return 1 if not targets or any("error" in v for v in outputs.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
