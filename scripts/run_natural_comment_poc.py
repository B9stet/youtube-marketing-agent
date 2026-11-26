"""Run the integrated comment PoC against saved scenes, without posting.

Generation uses the selected local connection. The optional review and verify
phases use the previously configured PoC Jev; other providers are not tested.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.natural_comment import GENERATION_PROMPT, JEV_QUESTIONS, compile_feedback, correct, diagnose, generate, route_signals
from scripts.run_jev_humanizer import prepare_existing_jev

SOURCE = ROOT / ".agent/v2-reference-perspectives-20261126/inputs.json"
RUN = ROOT / ".agent/natural-integrated-poc-20261126"
MODEL = "google-vertex/gemini-4-hydrogen"
BASE = "http://127.0.0.1:10100/v1"


def save(name: str, data: object) -> None:
    (RUN / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def generation_signature(case: dict, base: str, model: str) -> str:
    material = json.dumps({"case": case, "base": base, "model": model, "prompt": GENERATION_PROMPT}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def make_cases() -> list[dict]:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    cases = []
    for item in source:
        experience = item["reaction_label"] == "경험에 비춰보기"
        persona = ("영상의 상황을 보고 자기 연애의 기억을 떠올릴 수 있는 가상 시청자"
                   if experience else "영상 속 대화에 반응하며 선택한 입장을 갖는 가상 시청자")
        voice = ("실제 참고 댓글의 정서와 자연스러운 1인칭 회상 방식을 참고한다. 참고 작성자의 구체적 개인사는 복사하지 않는다."
                 if experience else item["voice"])
        if item["video_id"] == "8xAfwDQtDj8" and item["scene_id"] == "refusal" and not experience:
            closing = "거절을 존중하면서 엇갈린 마음에 관한 짧은 생각이나 감탄으로 이어갈 수 있다. 장면과 동떨어진 화제는 꺼내지 않는다."
        elif item["video_id"] == "6Hq96MZqego" and item["scene_id"] == "after" and not experience:
            closing = "연락의 선을 정하자는 의견 속에서도 이별의 여운을 존중한다. 아쉬움, 혼잣말 또는 의견으로 이어가되 쉽게 해결된다고 말하지 않는다."
        elif item["video_id"] == "n7ho4C1aKVw":
            closing = "장면에 맞는 가벼운 감탄이나 장난. '이래서 먼저 봐야 됨' 같은 표현도 자연스러울 때 사용한다."
        elif item["video_id"] == "Cr2XlCDrxUM" and item["scene_id"] == "difference":
            closing = "서로 다른 생각을 이해해도 같은 이유로 다시 부딪힐지에 관한 관심이나 의견. 기질로 갈등을 단순화하지 않는다."
        else:
            closing = "선택한 감정에서 자연스럽게 이어지는 의견, 질문, 아쉬움 또는 혼잣말. 마지막에 짧은 감상이 더 와도 된다."
        cases.append({"key": item["key"], "video_id": item["video_id"], "scene_id": item["scene_id"],
            "video_title": item["title"], "scene": "\n".join(item["scene_context"]),
            "scene_quote": item["quote"], "stance": item["reference_viewpoint"],
            "persona": persona, "voice": voice, "closing_preference": closing,
            "reference_comments": [{"text": item["reference_comment"], "kind": item["reference_kind"],
                                    "url": item["reference_url"]}],
            "reaction_label": item["reaction_label"], "fictional_experience_mode": experience})
    return cases


def generate_one(case: dict, base: str, model: str) -> dict:
    with httpx.Client(timeout=160) as client:
        first = generate(case, client, base_url=base, model=model)
        if "hydrogen" in model.lower() and first.get("model") and "hydrogen" not in str(first["model"]).lower():
            return {"key": case["key"], "error": "요청과 다른 모델이 응답했습니다.", "first": first}
        result = {"key": case["key"], "generation_signature": generation_signature(case, base, model), "first": first}
        if first.get("errors"):
            retry_case = {**case, "feedback": "이전 시안의 형식 문제를 고쳐 주세요: " + "; ".join(first["errors"])}
            second = generate(retry_case, client, base_url=base, model=model)
            result["retry"] = second
        chosen = result.get("retry", first)
        if chosen.get("answer", {}).get("action") == "draft" and chosen.get("comment") and not chosen.get("errors") and not chosen.get("error"):
            result["comment"] = chosen["comment"]
        else:
            result["error"] = "형식에 맞는 초안을 확보하지 못했거나 보류됐습니다."
        return result


def generation(base: str, model: str) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    cases = make_cases()
    save("inputs.json", cases)
    path = RUN / "generated.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    by_key = {case["key"]: case for case in cases}
    valid = {r["key"]: r for r in previous if r.get("comment") and r.get("key") in by_key
             and r.get("generation_signature") == generation_signature(by_key[r["key"]], base, model)}
    todo = [case for case in cases if case["key"] not in valid]
    print(f"통합 생성: {len(cases)}개 중 {len(todo)}개 실행", flush=True)
    (RUN / "generation-prompt-used.md").write_text(GENERATION_PROMPT, encoding="utf-8")
    records = list(valid.values())
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(generate_one, case, base, model): case for case in todo}
        for future in as_completed(futures):
            case = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"key": case["key"], "error": f"{type(exc).__name__}: {str(exc)[:150]}"}
            records.append(result)
            save("generated.json", records)
            print(case["key"], "생성 완료" if result.get("comment") else "보류", flush=True)


def paced_call(client: httpx.Client, case: dict, comment: str, others: list[str], last: float) -> tuple[dict, float]:
    delay = 8 - (time.monotonic() - last)
    if delay > 0:
        time.sleep(delay)
    for attempt in range(2):
        try:
            result = diagnose(case, comment, others, client)
            return result, time.monotonic()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            if attempt:
                raise RuntimeError(f"Jev 진단 실패: {type(exc).__name__}: {str(exc)[:120]}") from exc
            time.sleep(8)
    raise RuntimeError("Jev 진단 실패")


def review() -> None:
    cases = {case["key"]: case for case in json.loads((RUN / "inputs.json").read_text(encoding="utf-8"))}
    generated = json.loads((RUN / "generated.json").read_text(encoding="utf-8"))
    baseline_path = ROOT / ".agent/v2-reference-perspectives-20261126/results.json"
    baseline_results = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline = sorted((item["key"], (item["body"] + " " + item["ending"]).strip())
        for group in baseline_results for item in group.get("items", [])
        if item.get("action") == "draft" and item.get("body") and item.get("ending"))
    previous_path = RUN / "reviewed.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else []
    prior = {r["key"]: r for r in previous if r.get("diagnosis")}
    done = {}
    todo = []
    for record in sorted((r for r in generated if r.get("comment")), key=lambda x: x["key"]):
        target_video = record["key"].split("|", 1)[0]
        same_video = [(key, text) for key, text in baseline if key != record["key"] and key.split("|", 1)[0] == target_video]
        chosen = same_video[:1]
        seen_videos = {target_video}
        for key, text in baseline:
            video = key.split("|", 1)[0]
            if video not in seen_videos:
                chosen.append((key, text)); seen_videos.add(video)
            if len(chosen) >= 4:
                break
        others = [text for _, text in chosen]
        signature_input = json.dumps({"comment": record["comment"], "case": cases[record["key"]], "others": others, "questions": JEV_QUESTIONS}, ensure_ascii=False, sort_keys=True)
        signature = hashlib.sha256(signature_input.encode("utf-8")).hexdigest()
        old = prior.get(record["key"], {})
        if old.get("comparison_signature") == signature:
            done[record["key"]] = old
        else:
            todo.append((record, others, signature))
    prepare_existing_jev()
    out = list(done.values())
    last = 0.0
    print(f"기존 Jev 진단: {len(todo)}개", flush=True)
    with httpx.Client(timeout=60) as client:
        for record, others, signature in todo:
            case = cases[record["key"]]
            row = {"key": record["key"], "comment": record["comment"], "comparison_comments": others,
                   "comparison_signature": signature}
            try:
                row["diagnosis"], last = paced_call(client, case, record["comment"], others, last)
                row["plan"] = route_signals(row["diagnosis"]["scores"])
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                last = time.monotonic()
            out.append(row)
            save("reviewed.json", out)
            print(row["key"], row.get("plan", {}).get("action", "진단 실패"), flush=True)


def correct_one(row: dict, case: dict, base: str, model: str) -> dict:
    with httpx.Client(timeout=160) as client:
        result = correct(case, row["comment"], row["plan"]["selected"], row["comparison_comments"], client, base_url=base, model=model)
    return {"key": row["key"], "original": row["comment"], "selected": row["plan"]["selected"],
            "comparison_comments": row["comparison_comments"], "batch_repeat": row.get("batch_repeat", []),
            "selection_signature": row["selection_signature"], "correction": result}


def correction(base: str, model: str) -> None:
    cases = {case["key"]: case for case in json.loads((RUN / "inputs.json").read_text(encoding="utf-8"))}
    reviewed = json.loads((RUN / "reviewed.json").read_text(encoding="utf-8"))
    path = RUN / "corrected.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    prior = {r["key"]: r for r in previous if r.get("correction", {}).get("answer")}
    generated = [r for r in json.loads((RUN / "generated.json").read_text(encoding="utf-8")) if r.get("comment")]
    patterns = {"잘 본다던데": re.compile(r"잘\s*본다던데"), "궁금하다는 마무리": re.compile(r"궁금(?:해|하|했|하지)")}
    repeated = {}
    for label, pattern in patterns.items():
        matches = [r for r in generated if pattern.search(r["comment"].split("운명원", 1)[-1])]
        if len({r["key"].split("|", 1)[0] for r in matches}) >= 3:
            repeated[label] = matches
    prepared = []
    for row in reviewed:
        if row.get("plan", {}).get("action") in ("review_facts", "review_constraints"):
            continue
        repeated_labels = [label for label, matches in repeated.items() if any(x["key"] == row["key"] for x in matches)]
        selected = list(row.get("plan", {}).get("selected", []))
        if repeated_labels and "repetition" not in selected:
            selected.append("repetition")
        if selected:
            comparisons = list(row["comparison_comments"])
            for label in repeated_labels:
                candidates = [x["comment"] for x in repeated[label] if x["key"] != row["key"]]
                comparisons = (candidates[:3] + comparisons)[:6]
            prepared.append({**row, "plan": {**row["plan"], "action": "rewrite", "selected": selected},
                             "comparison_comments": comparisons, "batch_repeat": repeated_labels})
    done = {}
    todo = []
    for row in prepared:
        signature_input = json.dumps({"comment": row["comment"], "selected": sorted(row["plan"]["selected"]),
            "comparisons": row["comparison_comments"], "prompt": compile_feedback(row["plan"]["selected"])}, ensure_ascii=False, sort_keys=True)
        signature = hashlib.sha256(signature_input.encode("utf-8")).hexdigest()
        if prior.get(row["key"], {}).get("selection_signature") == signature:
            done[row["key"]] = prior[row["key"]]
        else:
            todo.append({**row, "selection_signature": signature})
    out = list(done.values())
    print(f"고정 규칙 교정: {len(todo)}개", flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(correct_one, row, cases[row["key"]], base, model): row for row in todo}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"key": row["key"], "error": f"{type(exc).__name__}: {str(exc)[:150]}"}
            out.append(result)
            save("corrected.json", out)
            print(result["key"], result.get("correction", {}).get("answer", {}).get("decision", "교정 실패"), flush=True)


def verification() -> None:
    cases = {case["key"]: case for case in json.loads((RUN / "inputs.json").read_text(encoding="utf-8"))}
    reviewed = {row["key"]: row for row in json.loads((RUN / "reviewed.json").read_text(encoding="utf-8"))}
    corrected = json.loads((RUN / "corrected.json").read_text(encoding="utf-8"))
    path = RUN / "verified.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    prior = {r["key"]: r for r in previous if r.get("diagnosis")}
    done = {}
    todo = []
    for record in corrected:
        if not (record.get("correction", {}).get("answer", {}).get("decision") == "rewrite"
                and record.get("correction", {}).get("comment") and not record["correction"].get("errors")):
            continue
        comment = record["correction"]["comment"]
        comparison = record.get("comparison_comments", reviewed[record["key"]]["comparison_comments"])
        signature_input = json.dumps({"comment": comment, "case": cases[record["key"]], "comparisons": comparison, "questions": JEV_QUESTIONS}, ensure_ascii=False, sort_keys=True)
        signature = hashlib.sha256(signature_input.encode("utf-8")).hexdigest()
        old = prior.get(record["key"], {})
        if old.get("verification_signature") == signature:
            done[record["key"]] = old
        else:
            todo.append((record, comparison, signature))
    prepare_existing_jev()
    out = list(done.values())
    last = 0.0
    print(f"교정 후 Jev 재진단: {len(todo)}개", flush=True)
    with httpx.Client(timeout=60) as client:
        for record, comparison, signature in todo:
            case = cases[record["key"]]
            comment = record["correction"]["comment"]
            row = {"key": record["key"], "comment": comment, "verification_signature": signature}
            try:
                row["diagnosis"], last = paced_call(client, case, comment, comparison, last)
                row["plan"] = route_signals(row["diagnosis"]["scores"])
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                last = time.monotonic()
            out.append(row)
            save("verified.json", out)
            print(row["key"], row.get("plan", {}).get("action", "진단 실패"), flush=True)


def replan() -> None:
    rows = json.loads((RUN / "reviewed.json").read_text(encoding="utf-8"))
    save("reviewed-before-routing-update.json", rows)
    for row in rows:
        if row.get("diagnosis"):
            row["plan"] = route_signals(row["diagnosis"]["scores"])
    save("reviewed.json", rows)
    print("진단 수치를 새 검토/교정 기준에 적용:", len(rows), "개", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("generate", "review", "replan", "correct", "verify"))
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    {"generate": lambda: generation(args.base, args.model), "review": review, "replan": replan,
     "correct": lambda: correction(args.base, args.model), "verify": verification}[args.phase]()
