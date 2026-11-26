"""Run generation → Jev → deterministic correction on selected video scenes.

All outputs are drafts for review. No browser or posting operations are used.
"""
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
from poc.contextual_brand_poc import BODY_PROMPT, CORRECTION_PROMPT, GENERATION_PROMPT, JEV_QUESTIONS, correct, diagnose, generate, plan_correction, post_review_status, rewrite_body
from poc.drafts import character_count
from poc.natural_variants import apply_variant
from scripts.run_jev_humanizer import prepare_existing_jev

RUN = ROOT / ".agent/contextual-bridge-poc" / datetime.now().strftime("run-%Y%m%d-%H%M%S")
RECENT = ["운명원 연애 사주도 궁금해지네요.", "운명원 사주가 생각나네요.", "운명원 사주로 성향을 짚어보면 좋겠어요."]

CASES = [
    {"id": "birthday", "title": "연애할 때 비참해지는 순간", "url": "https://www.youtube.com/watch?v=b7_QhneQL9E&t=305s", "source": "자동 자막, 기존 연결 시험 영상",
     "scene": "생일날 남자친구에게 축하 연락도 받지 못하고 기다렸으며 밤 11시가 넘어서야 전화가 왔다고 말한다.",
     "roles": "생일을 맞아 연락을 기다린 사람과 늦게 전화한 남자친구는 서로 다른 사람이다.", "evidence": "생일 축하한다는 연락도 없었어요",
     "body": "생일 축하 연락도 없다가 밤 11시 넘어서야 전화하는 건 진짜 너무했다. 저러면 서운할 수밖에 없음.",
     "stance": "연락을 기다린 사람의 서운함과 남자친구 행동에 대한 비판", "tone": "직설적인 구어체, 짧은 반말"},
    {"id": "reunion", "title": "끝이 보이는 연애", "url": "https://www.youtube.com/watch?v=C2LVY7NZeFU&t=720s", "source": "기존 검토 구간, 기존 연결 시험 영상",
     "scene": "과거에 헤어진 두 사람이 재회를 고민하며 다시 만나면 행복할지 이야기한다.",
     "roles": "두 사람의 재회에 대한 고민이다. 재회 여부나 미래 결과를 정해진 사실로 쓰지 않는다.", "evidence": "우리가 다시 만나면 행복할까?",
     "body": "다시 만나면 행복할까 고민하는 장면이 현실적이네요. 같은 이유로 또 헤어질까 봐 망설이는 마음이 이해돼요.",
     "stance": "마음이 남아 있지만 이전의 상처가 반복될까 망설이는 반응", "tone": "담담한 해요체"},
    {"id": "chemistry", "title": "72시간 소개팅 타이완 편", "url": "https://www.youtube.com/watch?v=IOz2MKthMJA&t=2462s", "source": "자동 자막 및 기존 시청자 댓글, 기존 연결 시험 영상",
     "scene": "더운 날 거울을 보고 늙어 보인다고 하자 다른 출연자가 똑같이 예쁘다고 말하며 대화를 이어 간다.",
     "roles": "외모를 걱정한 출연자와 예쁘다고 받아준 출연자는 서로 다른 사람이다.", "evidence": "똑같이 예쁘다고",
     "body": "더워서 늙어 보인다고 하니까 똑같이 예쁘다고 받아주는 거 귀엽네요ㅋㅋ 둘이 티키타카가 좋다.",
     "stance": "두 사람의 대화에 대한 가벼운 호감과 즐거움", "tone": "가벼운 말투, ㅋㅋ 허용"},
    {"id": "wait_in_cold", "title": "남친이 내 존재를 숨기는 이유", "url": "https://www.youtube.com/watch?v=ku4sejh-O4s&t=24s", "source": "수동 자막, 이 연결 시험에서 처음 사용",
     "scene": "남자친구가 30분 늦어 여성이 추운 곳에서 기다렸다. 남자는 처음 만났을 때와 이후에 여성에게 미안하다고 말한 뒤, 주변 사람들에게도 죄송하다고 한다. 여성은 다른 사람들의 시선보다 자신에 대한 사과가 먼저 아니냐고 말한다.",
     "roles": "남자친구가 늦었다. 여성은 30분을 기다렸다. 남자가 여성에게 사과한 말이 주변 사람들에게 죄송하다고 한 말보다 먼저 나온다.", "evidence": "나한테 미안해야 되는 게 먼저 아니야?",
     "body": "추운데 30분이나 기다린 입장에선, 사과를 들었더라도 남의 시선을 먼저 신경 쓰는 모습이 서운할 수 있겠어요.",
     "stance": "기다린 사람의 서운함에 공감하되 남자친구가 사과하지 않았다고 단정하지 않음", "tone": "차분하지만 답답한 해요체"},
    {"id": "speaking_carefully", "title": "남자친구 짜증 내는 말투 고치는 법", "url": "https://www.youtube.com/watch?v=ZtoxofTKy7I&t=44s", "source": "자동 자막, 이 연결 시험에서 처음 사용",
     "scene": "사연자는 남자친구를 많이 좋아하지만 계속되는 짜증스러운 말투 때문에 자신의 의견을 말하기 어려워졌고 헤어짐을 고민한다고 이야기한다.",
     "roles": "사연자가 의견을 말하기 어려워졌다. 짜증스러운 말투는 남자친구의 말투로 설명된다.", "evidence": "제 의견도 제대로 말 못 하게 되었고",
     "body": "많이 좋아하는데도 짜증 섞인 말투 때문에 자기 의견을 말하기 어려워졌다니... 헤어짐을 생각하는 마음이 막막하겠어요.",
     "stance": "관계의 양가감정과 사연자의 어려움에 공감, 이별을 단정하지 않음", "tone": "걱정이 담긴 해요체"},
    {"id": "past_memories", "title": "전 여친 다시 만나고 싶은 순간", "url": "https://www.youtube.com/watch?v=Tz3Hagf0WMI&t=25s", "source": "자동 자막, 이 연결 시험에서 처음 사용",
     "scene": "출연자는 다른 사람의 연애를 보거나 옛 사진과 추억을 마주하면 예전에 만났던 사람이 떠오른다고 말한다.",
     "roles": "현재 과거 연인이 떠오른다는 말이다. 다시 만나기로 했다는 사실은 없다.", "evidence": "한 때 만났던 사람이니까 생각나는 게 당연하지",
     "body": "예전에 만났던 사람이니까 생각나는 게 당연하다고 넘기려는 장면이 씁쓸하네요. 추억 앞에서는 마음이 다시 흔들리기도 하죠.",
     "stance": "이별 뒤의 그리움을 이해하지만 재회를 단정하지 않음", "tone": "차분하고 여운 있는 해요체"},
]


def prepare_cases() -> list[dict]:
    cases = []
    for source in CASES:
        case = dict(source)
        case["recent_endings"] = RECENT
        cases.append(case)
    return cases


def probes(cases: list[dict]) -> list[dict]:
    by_id = {c["id"]: c for c in cases}
    output = []
    for key, ending, label in (
        ("birthday", "운명원에서 사주라도 미리 봤으면 좋았을텐데.", "사용자가 가볍다고 지적한 마지막 문장"),
        ("reunion", "운명원에서 사주라도 보고 마음 편하게 결정했으면 좋겠어요.", "사용자가 실제 갈등에 맞지 않는다고 지적한 마지막 문장"),
    ):
        case = dict(by_id[key])
        case["id"] = key + "_criticized"
        output.append({"case": case, "kind": "criticized_control", "label": label,
                       "initial": {"ending": ending, "comment": case["body"] + " " + ending,
                                   "emotional_core": "", "bridge_reason": "", "errors": check_result(case["body"], ending)}})
    case = dict(by_id["birthday"])
    case.update({"id": "birthday_flat_body", "body": "생일에 연락이 없었던 부분이 조금 아쉽네요.",
                 "stance": "서운함에 공감하는 시선"})
    ending = "운명원 사주도 궁금해지네요."
    output.append({"case": case, "kind": "body_control", "label": "본문 재작성 분기를 살피기 위해 준비한 가벼운 반응",
                   "initial": {"ending": ending, "comment": case["body"] + " " + ending,
                               "emotional_core": "", "bridge_reason": "", "errors": check_result(case["body"], ending)}})
    return output


def dump(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save(records: list[dict]) -> None:
    dump(RUN / "results.json", records)


def generate_one(case: dict) -> dict:
    with httpx.Client() as client:
        return generate(case, client)


def as_initial(result: dict) -> dict:
    answer = result.get("answer", {})
    return {"ending": answer.get("ending", ""), "comment": result.get("comment", ""),
            "emotional_core": answer.get("emotional_core", ""), "bridge_reason": answer.get("bridge_reason", ""),
            "errors": result.get("errors", [])}


def main() -> None:
    RUN.mkdir(parents=True, exist_ok=False)
    (RUN / "generation-prompt.md").write_text(GENERATION_PROMPT, encoding="utf-8")
    (RUN / "correction-prompt.md").write_text(CORRECTION_PROMPT, encoding="utf-8")
    (RUN / "body-prompt.md").write_text(BODY_PROMPT, encoding="utf-8")
    dump(RUN / "jev-questions.json", JEV_QUESTIONS)
    cases = prepare_cases()
    records = probes(cases) + [{"case": case, "kind": "generated"} for case in cases]
    save(records)
    print(f"저장 위치: {RUN}", flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(generate_one, record["case"]): record for record in records if record["kind"] == "generated"}
        for future in as_completed(tasks):
            record = tasks[future]
            try:
                result = future.result()
                record["generation"] = result
                if result.get("answer", {}).get("action") == "draft" and "error" not in result:
                    record["initial"] = as_initial(result)
            except Exception as exc:
                record["generation"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            save(records)
            print(f"생성 {record['case']['id']}: {record['generation'].get('answer', {}).get('action', 'error')}", flush=True)

    prepare_existing_jev()
    last_call = 0.0
    jev_available = True
    with httpx.Client(timeout=120) as client:
        def paced_diagnose(case, initial):
            nonlocal last_call
            wait = 8 - (time.monotonic() - last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                return diagnose(case, initial["comment"], initial["ending"], RECENT, client)
            finally:
                last_call = time.monotonic()

        def regenerate_after_body(record, case):
            nonlocal jev_available
            if "body_rewrite" in record:
                return {"status": "hold", "reason": "본문을 한 번 교정한 뒤에도 재검토가 필요합니다."}
            record["body_rewrite"] = rewrite_body(case, client)
            rewritten = record["body_rewrite"].get("body")
            if not rewritten:
                return {"status": "hold", "reason": "본문 재작성에 실패했습니다."}
            changed_case = {**case, "original_body": case["body"], "body": rewritten}
            record["rewritten_case"] = changed_case
            record["regeneration"] = generate(changed_case, client)
            regen = record["regeneration"]
            if regen.get("answer", {}).get("action") != "draft" or "error" in regen:
                return {"status": "hold", "reason": "본문 재작성 후 브랜드 문장 생성이 보류됐습니다."}
            fresh = as_initial(regen)
            record["regenerated_initial"] = fresh
            try:
                record["post_diagnosis"] = paced_diagnose(changed_case, fresh)
                record["post_plan"] = plan_correction(record["post_diagnosis"]["scores"], fresh["errors"], body_changed=True)
            except Exception as exc:
                record["post_diagnosis"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                jev_available = False
            status = post_review_status(record["post_plan"], body_changed=True, scores=record["post_diagnosis"]["scores"]) if "post_plan" in record else "hold"
            return {"status": status, "comment": fresh["comment"],
                    "reason": "본문이 변경되어 원문 대비 검토가 필요합니다."}

        for record in records:
            case = record["case"]
            gen_action = record.get("generation", {}).get("answer", {}).get("action")
            initial = record.get("initial")
            if not initial and gen_action == "revise_body":
                record["body_rewrite"] = rewrite_body(case, client)
                rewritten = record["body_rewrite"].get("body")
                if rewritten:
                    changed_case = {**case, "original_body": case["body"], "body": rewritten}
                    record["rewritten_case"] = changed_case
                    record["regeneration"] = generate(changed_case, client)
                    if record["regeneration"].get("answer", {}).get("action") == "draft" and "error" not in record["regeneration"]:
                        case = changed_case
                        record["initial"] = initial = as_initial(record["regeneration"])
            if not initial:
                record["outcome"] = {"status": "hold", "reason": "생성 단계에서 연결을 보류했거나 응답 조건을 충족하지 못했습니다."}
                save(records)
                print(f"결과 {case['id']}: hold", flush=True)
                continue
            if not jev_available:
                record["outcome"] = {"status": "diagnosis_unavailable", "reason": "무료 Jev 요청이 중단되어 검토가 필요합니다."}
                save(records)
                continue
            try:
                record["diagnosis"] = paced_diagnose(case, initial)
            except Exception as exc:
                record["diagnosis"] = {"error": f"{type(exc).__name__}: {str(exc)[:250]}"}
                record["outcome"] = {"status": "diagnosis_unavailable", "reason": "Jev 진단 요청 실패"}
                jev_available = False
                save(records)
                print(f"Jev 중단 {case['id']}: {type(exc).__name__}", flush=True)
                continue
            plan = plan_correction(record["diagnosis"]["scores"], initial.get("errors"), confirmed_source_errors=record.get("confirmed_source_errors"), body_changed=case.get("original_body", case["body"]) != case["body"])
            record["plan"] = plan
            print(f"진단 {case['id']}: {plan['action']} / {','.join(plan['selected']) or '-'}", flush=True)
            if plan["action"] == "revise_body":
                record["outcome"] = regenerate_after_body(record, case)
            elif plan["action"] == "rewrite":
                record["correction"] = correct(case, initial, plan, client)
                correction = record["correction"]
                decision = correction.get("answer", {}).get("decision")
                if decision == "rewrite" and not correction.get("errors") and "error" not in correction:
                    changed = {"ending": correction["ending"], "comment": correction["comment"]}
                    try:
                        record["post_diagnosis"] = paced_diagnose(case, changed)
                        record["post_plan"] = plan_correction(record["post_diagnosis"]["scores"], [], body_changed=case.get("original_body", case["body"]) != case["body"])
                        status = post_review_status(record["post_plan"], body_changed=case.get("original_body", case["body"]) != case["body"], scores=record["post_diagnosis"]["scores"])
                        reason = {"hold": "화자, 사실 또는 형식에 대한 재검토가 필요합니다.",
                                  "review_correction": "교정 후에도 탐색 신호가 남아 있습니다.",
                                  "review_body_rewrite": "변경된 본문을 원문과 대조해야 합니다.",
                                  "review_draft": "Jev에서 탐색 기준 이상의 신호가 없으며 문맥 확인이 필요합니다."}[status]
                        record["outcome"] = {"status": status, "comment": correction["comment"], "reason": reason}
                    except Exception as exc:
                        record["post_diagnosis"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                        jev_available = False
                        record["outcome"] = {"status": "review_correction", "comment": correction["comment"], "reason": "교정 후 Jev 확인 실패"}
                elif decision == "revise_body" and "error" not in correction:
                    record["outcome"] = regenerate_after_body(record, case)
                elif decision == "keep" and not correction.get("errors") and "error" not in correction:
                    status = "hold" if record["diagnosis"]["scores"]["stance"] >= .75 else "review_correction"
                    record["outcome"] = {"status": status, "comment": initial["comment"], "reason": "Jev가 표시했지만 LLM이 원문을 유지해 교정 여부를 직접 검토합니다."}
                else:
                    record["outcome"] = {"status": "hold", "reason": f"교정 단계 결과: {decision or 'error'}"}
            elif plan["action"] == "keep":
                record["outcome"] = {"status": "review_draft", "comment": initial["comment"], "reason": "탐색 신호는 낮지만 문맥을 확인합니다."}
            else:
                record["outcome"] = {"status": "hold", "reason": plan["reason"]}
            if record.get("rewritten_case") and record["outcome"].get("comment") and record["outcome"]["status"] != "hold":
                record["outcome"]["status"] = "review_body_rewrite"
                record["outcome"]["reason"] = "본문이 변경되어 원문 대비 검토가 필요합니다."
            if record["kind"] == "generated" and record["outcome"].get("comment") and record["outcome"]["status"] in ("review_draft", "review_correction"):
                record["variant"] = apply_variant(record["outcome"]["comment"], evidence_quote=case.get("evidence", ""), seed="contextual-poc-20261125", case_id=case["id"], rate=.30)
                if record["variant"].get("applied"):
                    record["outcome"]["comment"] = record["variant"]["comment"]
                    record["outcome"]["reason"] += " 한 곳의 선택적 표기 변형을 적용했습니다."
                record["outcome"]["characters"] = character_count(record["outcome"]["comment"])
            save(records)
            print(f"완료 {case['id']}: {record['outcome']['status']}", flush=True)
    print(f"끝: {len(records)}건, {RUN}", flush=True)


if __name__ == "__main__":
    main()
