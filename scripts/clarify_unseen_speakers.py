"""Apply a manually triggered, fixed speaker-clarity rule to two draft candidates."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import re
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poc.jev_humanizer import rewrite, route_revision, verify_revision
from poc.natural_variants import apply_variant
from scripts.run_jev_humanizer import json_read, json_write, outcome_error, prepare_existing_jev
from scripts.run_unseen_video_poc import WORK, REPORT, build_cases, paced, render

TARGETS = ("XbPld5XoD7Y_brief_informal", "XbPld5XoD7Y_practical_advice", "XbPld5XoD7Y_question_reaction")
ROLE_CONTEXT = {
    "화난_사람": "'네가 나랑 한 약속을 어겨서 화가 난 거야'라고 말하는 사람",
    "약속을_어긴_사람": "이 말을 들은 상대방",
    "근거_표현": "약속을 어겨서",
    "주의": "'상대방이 약속을 어겨서 서운하다고'라고 쓰면 약속을 어긴 사람과 서운한 사람의 주체가 뒤섞인다. 서운하다고 말한 사람과 약속을 어긴 사람을 분리한다.",
}


def role_ambiguity_present(comment: str) -> bool:
    """Detect the observed role ambiguity for this specific video scene."""
    return bool(re.search(r"상대방이\s+(?:나랑\s+한\s+)?약속을\s+어겨서\s+서운하다고", comment))


def role_context_for(case_id: str) -> dict[str, str]:
    context = dict(ROLE_CONTEXT)
    if case_id == "XbPld5XoD7Y_practical_advice":
        context["조언의_대상"] = "약속을 어긴 상대가 먼저 사과하라는 취지"
    return context


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--pace-seconds", type=float, default=8.0)
    parser.add_argument("--casual-rate", type=float, default=0.30)
    parser.add_argument("--only", action="append", choices=TARGETS, help="특정 시안만 추가 교정")
    args = parser.parse_args()
    if not 0 <= args.pace_seconds <= 60 or not 0 <= args.casual_rate <= 1:
        raise ValueError("요청 간격 또는 표기 변형 비율이 올바르지 않습니다.")
    run = args.run.resolve()
    if not run.is_relative_to(WORK.resolve()) or not (run / "results.json").is_file():
        raise ValueError("기존 새 영상 PoC 실행 폴더가 필요합니다.")
    cases = build_cases()
    if json_read(run / "cases.json") != cases:
        raise ValueError("원본 영상 자료가 달라져 기존 진단을 재사용할 수 없습니다.")
    by_id = {case["case_id"]: case for case in cases}
    records = json_read(run / "results.json")
    targets = tuple(args.only) if args.only else TARGETS
    probe_path = run / "role-probe.json"
    if probe_path.is_file():
        records[TARGETS[1]]["actor_probe"] = json_read(probe_path)
    prepare_existing_jev()
    last_jev = [0.0]
    with httpx.Client() as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            for case_id in targets:
                case = by_id[case_id]
                record = records[case_id]
                record["actor_review"] = "별도 검토에서 영상 대사의 '나랑'이 섞여 화자와 행동 주체가 헷갈릴 수 있다고 판단했다."
                if record.get("manual_correction"):
                    record.setdefault("manual_correction_history", []).append(record["manual_correction"])
                material = {"comment": record["generation"]["comment"], "evidence_quote": case["quote"],
                            "scene": case["scene"], "role_context": role_context_for(case_id)}
                futures[pool.submit(rewrite, material, {"action": "rewrite", "selected": ["actor_clarity"]}, client=client)] = case_id
            for index, future in enumerate(as_completed(futures), 1):
                case_id = futures[future]
                try:
                    value = future.result()
                except Exception as exc:
                    value = {"error": outcome_error(exc)}
                records[case_id]["manual_correction"] = value
                json_write(run / "manual-actor-correction" / f"{case_id}.json", value)
                print(f"화자 명확화 {index}/{len(targets)}: {case_id} {value.get('error', '완료')}", flush=True)
        changed = [case_id for case_id in targets if records[case_id].get("manual_correction", {}).get("changed")]
        for index, case_id in enumerate(changed, 1):
            record = records[case_id]
            try:
                value = paced(lambda: verify_revision(record["generation"]["comment"], record["manual_correction"]["comment"], client=client), last_jev, args.pace_seconds)
            except Exception as exc:
                value = {"error": outcome_error(exc)}
            if record.get("manual_verification"):
                record.setdefault("manual_verification_history", []).append(record["manual_verification"])
            record["manual_verification"] = value
            if "scores" in value:
                record["manual_route"] = route_revision(record["manual_correction"].get("errors", []), value["scores"])
            json_write(run / "manual-actor-verification" / f"{case_id}.json", value)
            print(f"화자 교정 확인 {index}/{len(changed)}: {case_id} {value.get('error', '완료')}", flush=True)
            if value.get("error") == "HTTP 429":
                break
    for case_id in targets:
        record = records[case_id]
        case = by_id[case_id]
        edit = record.get("manual_correction", {})
        ambiguous = role_ambiguity_present(edit.get("comment", ""))
        record["manual_role_ambiguity"] = ambiguous
        if edit.get("changed") and record.get("manual_route") == "no_issue_detected" and not ambiguous:
            record["variant"] = apply_variant(edit["comment"], evidence_quote=case["quote"], case_id=case_id, rate=args.casual_rate)
            record["final_note"] = "별도 검토 뒤 추가 교정한 후보이며, 최종 문맥 확인이 필요함"
        else:
            record.pop("variant", None)
            if ambiguous:
                record["final_note"] = "화자 혼동이 남아 원문과 추가 수정안을 함께 검토해야 함"
            elif edit.get("errors"):
                record["final_note"] = "교정문이 근거 표현 또는 글자 수 검사에 걸려 검토해야 함"
            else:
                record["final_note"] = "추가 교정의 의미 보존을 검토해야 함"
    json_write(run / "results.json", records)
    started = datetime.strptime(run.name, "%Y%m%d-%H%M%S").astimezone().isoformat(timespec="seconds")
    REPORT.write_text(render(cases, records, started), encoding="utf-8")
    print(f"갱신된 보고서: {REPORT}", flush=True)
    return 0 if all(not records[case_id].get("manual_correction", {}).get("error") for case_id in targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
