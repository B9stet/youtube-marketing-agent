"""Compare optional spelling/spacing variants with saved video comment drafts."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
import re
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poc.drafts import character_count, validate_comment
from poc.jev_humanizer import diagnose
from poc.natural_variants import apply_variant
from scripts.run_jev_humanizer import collect_cases, json_read, json_write, outcome_error, prepare_existing_jev, require_same_cases

PREVIOUS_ROOT = ROOT / ".agent" / "jev-humanizer-poc"
VIDEO_DIR = ROOT / ".agent" / "comment-research-2026-11-24" / "videos"
WORK = ROOT / ".agent" / "natural-variants-poc"
REPORT = ROOT / "output" / f"natural-writing-poc-{datetime.now():%Y-%m-%d}.md"
PATTERNS = {
    "것같/거같": r"(?:것|거)같",
    "할수/볼수 등": r"(?:할|될|볼|갈|알)수",
    "거에요": r"거에요",
    "좋겟": r"좋겟",
    "더라구": r"더라구",
    "맘": r"맘",
}


def original_viewer_comments() -> list[dict]:
    rows = []
    for path in sorted(VIDEO_DIR.glob("*.json")):
        video = json_read(path)
        for comment in video.get("comments", []):
            text = " ".join(str(comment.get("text", "")).split())
            if comment.get("is_uploader") or len(re.findall(r"[가-힣]", text)) < 4:
                continue
            rows.append({"video_id": video["video_id"], "comment_id": comment.get("id", ""), "text": text})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=float, default=0.30, help="표기 변형을 시도할 임시 확률")
    parser.add_argument("--pace-seconds", type=float, default=8.0)
    parser.add_argument("--skip-jev", action="store_true", help="기존 진단과의 비교를 생략한다")
    parser.add_argument("--source-run", type=Path, help="기존 Jev 진단 실행 폴더")
    args = parser.parse_args()
    if not 0 <= args.rate <= 1 or not 0 <= args.pace_seconds <= 60:
        raise ValueError("변형 비율 또는 Jev 요청 간격이 올바르지 않습니다.")
    if args.source_run:
        previous = args.source_run.resolve()
    else:
        paths = sorted(p for p in PREVIOUS_ROOT.iterdir() if p.is_dir() and (p / "results.json").is_file())
        if not paths:
            raise RuntimeError("비교할 Jev 진단 결과가 없습니다.")
        previous = paths[-1].resolve()
    if not previous.is_relative_to(PREVIOUS_ROOT.resolve()) or not (previous / "results.json").is_file():
        raise ValueError("기존 Jev 진단 폴더를 지정해야 합니다.")
    cases = collect_cases()
    require_same_cases(previous, cases)
    cases = [case for case in cases if case["kind"] == "generated"]
    prior = json_read(previous / "results.json")["diagnoses"]
    variants = {case["case_id"]: apply_variant(case["comment"], evidence_quote=case["evidence_quote"],
                                              case_id=case["case_id"], rate=args.rate) for case in cases}
    changed = [case for case in cases if variants[case["case_id"]]["applied"]]
    run = WORK / datetime.now().strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=False)
    json_write(run / "variants.json", {"rate": args.rate, "variants": variants})
    newer = {}
    if not args.skip_jev and changed:
        prepare_existing_jev()
        with httpx.Client() as client:
            for index, case in enumerate(changed, 1):
                adapted = {**case, "comment": variants[case["case_id"]]["comment"]}
                try:
                    newer[case["case_id"]] = diagnose(adapted, client=client)
                except Exception as exc:
                    newer[case["case_id"]] = {"error": outcome_error(exc)}
                json_write(run / "diagnosis" / f"{case['case_id']}.json", newer[case["case_id"]])
                print(f"변형 후 진단 {index}/{len(changed)}: {case['case_id']} {newer[case['case_id']].get('error', '완료')}", flush=True)
                if newer[case["case_id"]].get("error") == "HTTP 429":
                    break
                if index < len(changed) and args.pace_seconds:
                    time.sleep(args.pace_seconds)
    json_write(run / "results.json", {"rate": args.rate, "variants": variants, "diagnoses_after": newer})
    observed = original_viewer_comments()
    sample_counts = {label: sum(bool(re.search(pattern, row["text"])) for row in observed)
                     for label, pattern in PATTERNS.items()}
    kinds = Counter(variants[c["case_id"]]["label"] for c in changed)
    tokens = sum(int((v.get("usage") or {}).get("input_tokens") or 0) for v in newer.values())
    passed = sum(not validate_comment(variants[c["case_id"]]["comment"]) for c in changed)
    lines = ["# 댓글의 자연스러운 표기 변형 PoC", "",
             f"실행: {datetime.now().astimezone().isoformat(timespec='seconds')}", "",
             "## 목적과 적용 방식", "",
             "기존 댓글의 문맥과 입장을 유지하면서, 실제 시청자 댓글에서 발견한 띄어쓰기 및 표기 변형을 일부 초안에 적용한다. 기존 AI 말투 교정이 끝난 뒤 적용하므로 변형을 다시 맞춤법 교정으로 없애지 않는다.", "",
             f"- 시험용 시도 비율: {args.rate:.0%}. 댓글당 최대 한 군데이며, 적용할 수 있는 안전한 위치가 없으면 원문을 유지한다.",
             "- 따옴표 안 표현, 댓글에 포함된 영상 근거의 공통 구간, 브랜드나 사주가 있는 문장은 보호한다. 글자 수, 링크와 해시태그 등 기존 조건도 재검사한다.",
             "- 변형은 미리 정한 규칙으로 적용하며 LLM 추가 호출은 없다. Jev는 변형 후의 말투 판단이 달라지는지 비교하기 위해서만 호출했다.", "",
             "## 수집된 시청자 댓글에서 본 표기", "",
             f"기존에 확보한 시청자 댓글 {len(observed)}개에서 해당 문자열이 한 번 이상 나온 댓글 수다. 특정 문자열만 센 것으로 전체 오타의 비율은 아니다.", ""]
    for label, count in sample_counts.items():
        lines.append(f"- {label}: {count}개")
    lines += ["", "예를 들어 실제 댓글에서 [‘데이트햇음좋겟다’](https://www.youtube.com/watch?v=C2LVY7NZeFU&lc=UgwtMvVzYAfYDxFwL2d4AaABAg), [‘있는거같아요’와 ‘할수있고’](https://www.youtube.com/watch?v=JqW9PxxvuAA&lc=Ugwv8WNxrPBA5ZgF-Ph4AaABAg), [‘하더라구요’](https://www.youtube.com/watch?v=erMw0-yYWFE&lc=UgzjWmXjHVvyb4Krpa14AaABAg)가 관찰됐다.", "",
              "## 적용 결과", "",
              f"- 기존 초안 {len(cases)}개 중 {len(changed)}개에 적용됐다. 시도 비율과 실제 적용 비율은 다를 수 있다.",
              f"- 적용 유형: {', '.join(f'{name} {count}개' for name, count in kinds.items()) or '없음'}. 변형 후 기존 형식 검사 통과: {passed}/{len(changed)}개.",
              f"- 변형 후 Jev 추가 진단: {sum('error' not in v for v in newer.values())}/{len(changed)}건, 보고된 입력 토큰 {tokens:,}개.",
              "- 이 결과는 사람이 읽고 판단하는 비교 자료다. 문법적으로 틀린 표기가 무조건 더 자연스럽다는 평가나 최적 비율을 뜻하지 않는다.", ""]
    for case in changed:
        result = variants[case["case_id"]]
        old = prior.get(case["case_id"], {})
        fresh = newer.get(case["case_id"])
        lines += [f"### {case['title']} / {case['method']}", "", f"- 원문: {case['comment']}",
                  f"- 표기 변형: {result['comment']}", f"- 적용: {result['label']} (`{result['before']}` → `{result['after']}`)"]
        if fresh and "error" not in fresh:
            lines.append(f"- Jev의 어색한 한국어 신호: {old.get('scores', {}).get('awkward', 0):.2f} → {fresh['scores']['awkward']:.2f} (각 1회 진단)")
        elif fresh:
            lines.append(f"- Jev 비교 오류: {fresh['error']}")
        lines.append("")
    lines += ["## 판정할 때 참고할 점", "",
              "- 변형률 30%는 사용자의 ‘일부’라는 요구를 시험하기 위한 임시값이다. 수집 표본에서 오타 비율을 추정해 정한 수치가 아니다.",
              "- 문맥에 없는 경험과 감정을 더하지 않으며, 감정이나 입장을 바꾸는 치환은 대상에서 제외한다. 공감, 비판, 조언처럼 반응의 성격은 원문을 따른다.",
              "- `맘`은 오타가 아니라 구어체 축약으로 별도 기록한다. 스타일 변화가 부자연스러운지는 전후 문장을 사람이 읽고 평가해야 한다.", ""]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"보고서: {REPORT}", flush=True)
    print(f"원문 {len(cases)}개 중 변형 {len(changed)}개; 입력 검사 통과 {passed}개", flush=True)
    return 0 if args.skip_jev or (len(newer) == len(changed) and all("error" not in x for x in newer.values())) else 1


if __name__ == "__main__":
    raise SystemExit(main())
