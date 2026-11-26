"""Context-sensitive draft and Jev diagnostic primitives for a review-only PoC."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

import httpx

from .brand_bridge_compiler import check_result, compile_issues, select_from_signals
from .config import AI_BASE_URL, AI_MODEL
from .drafts import _response_text

ROOT = Path(__file__).resolve().parents[1]
GENERATION_PROMPT = (ROOT / "docs/prompts/contextual-bridge-generation.md").read_text(encoding="utf-8")
CORRECTION_PROMPT = (ROOT / "docs/prompts/contextual-bridge-correction.md").read_text(encoding="utf-8")
BODY_PROMPT = (ROOT / "docs/prompts/contextual-bridge-body.md").read_text(encoding="utf-8")

# Each Noul signal is independent. They are exploratory judgment signals, not
# calibrated probabilities of a defect or reliable measures of human preference.
JEV_QUESTIONS = {
    "emotion_flattened": "전체 댓글의 마지막 운명원 문장이 앞의 구체적 서운함, 갈등이나 망설임을 가벼운 불편, 단순 호기심 또는 쉬운 후회로 축소하는가? 상황에 어울리는 희망이나 질문이라는 이유만으로 판단하지 않는다.",
    "easy_resolution": "원래 장면의 어려운 감정이나 선택을 마지막 문장이 너무 쉽게 해결되거나 마음 편히 끝날 수 있는 것처럼 다루는가? 명시적인 해결 주장이 아니어도 맥락상 지나치게 간단한 결말이면 해당한다. 그 문제를 알 수 있는지 열린 질문을 하는 것 자체는 해결을 확정한 결말이 아니다.",
    "interest_gap": "마지막 문장이 운명원 사주를 살펴보고 싶은 이유나 관심을 전혀 드러내지 않고 브랜드명만 무관하게 넣거나, 사주가 무의미하다고만 일축하는가? 직접적인 구매 권유나 상품 설명이 없다는 이유만으로 판단하지 않는다.",
    "jump": "마지막 운명원 문장이 바로 앞 영상 반응과 구체적인 생각이나 감정의 이어짐 없이 갑자기 등장하는가? 브랜드가 언급된 사실만으로 판단하지 않는다.",
    "stance": "마지막 문장이 본문의 비판, 분노, 조언, 응원 또는 망설임의 입장을 뒤집거나 구체적 잘못의 책임을 성향이나 운명 탓으로 흐리는가?",
    "role_error": "댓글이 제공된 역할 설명이나 영상 장면에서 누가 어떤 행동을 했는지, 누가 그 행동에 반응했는지를 바꾸거나 심각하게 혼동하는가? 사실 확인이 부족하면 넘겨짚지 않는다.",
    "timeline_error": "댓글이 제공된 영상의 발언이나 행동 순서를 뒤바꾸거나, 이미 한 행동을 하지 않았다고 단정하는가? 자막이나 역할 설명에 없는 순서는 추측하지 않는다.",
    "claim": "댓글이 실제로 운명원을 이용한 경험, 확인되지 않은 서비스 기능이나 효과, 등장인물의 속마음 또는 미래를 사실로 만들어 단정하는가? 실제 이용을 주장하지 않는 가정이나 바람은 후기가 아니다. 사주로 어떤 문제를 알 수 있는지 열린 질문을 하는 것 자체는 답이나 기능을 단정한 것이 아니다.",
    "advertorial": "마지막 문장에 일상적인 시청자 반응보다 상담 안내문이나 광고문처럼 읽히는 장황하고 추상적인 말이 있는가? 브랜드가 있다는 것만으로 판단하지 않는다.",
    "repetition": "제공된 recent_endings가 있을 때, 이 댓글의 마지막 문장이 다른 문구들 중 적어도 두 개와 구체적인 생각과 문장 구조까지 비슷해 상투적으로 반복되는가? 운명원, 사주라는 단어, 존댓말, 물음표가 공통이라는 이유만으로는 해당하지 않는다. 목록이 없으면 아니오다.",
    "body_flat": "브랜드를 제외한 반응 본문 자체가 영상의 구체적 사건과 선택된 반응을 심각하게 지우거나 깊은 상처나 갈등을 단순하고 가벼운 불편으로 바꾸는가? 짧거나 담담하다는 이유만으로 판단하지 않는다.",
    "body_stance": "반응 본문이 original_body와 다를 때 기존의 공감, 비판, 조언이나 망설임의 방향을 바꾸거나 없던 구체적인 사실을 단정하는가? 두 본문이 같으면 아니오다.",
}


def _signal(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Jev 신호는 0~1 사이의 유한한 수여야 합니다.")
    return float(value)


def confirmed_source_errors(entries: list[dict], case_id: str, comment: str) -> list[str]:
    """Apply manual source findings only to the exact reviewed draft."""
    fingerprint = hashlib.sha256(comment.encode("utf-8")).hexdigest()
    result = []
    for entry in entries:
        if entry.get("case_id") == case_id and entry.get("comment_sha256") == fingerprint:
            result.extend(str(error) for error in entry.get("errors", []) if str(error).strip())
    return result


def plan_correction(scores: dict[str, Any], errors: list[str] | None = None, *, confirmed_source_errors: list[str] | None = None, body_changed: bool = True, low: float = .35, high: float = .75) -> dict:
    """Select fixed rules in a stable order. Thresholds are provisional."""
    optional = {"interest_gap", "timeline_error"}
    if not 0 <= low < high <= 1 or not (set(JEV_QUESTIONS) - optional).issubset(scores) or set(scores) - set(JEV_QUESTIONS):
        raise ValueError("Jev 신호 전체와 유효한 탐색 기준이 필요합니다.")
    checked = {k: _signal(v) for k, v in scores.items()}
    mapped = {k: v for k, v in checked.items() if k not in ("body_flat", "body_stance")}
    selected = select_from_signals(mapped, low)
    if errors:
        return {"action": "review_constraints", "selected": selected, "reason": "형식 검사에 실패했습니다."}
    if confirmed_source_errors:
        return {"action": "review_facts", "selected": selected, "reason": "자막 대조에서 확인된 사건 또는 화자 오류가 있습니다."}
    if checked["role_error"] >= low or checked.get("timeline_error", 0) >= low or (body_changed and checked["body_stance"] >= low) or checked["claim"] >= high:
        return {"action": "review_facts", "selected": selected, "reason": "역할, 사실 또는 입장 보존을 확인해야 합니다."}
    if checked["body_flat"] >= low:
        return {"action": "revise_body", "selected": selected, "reason": "본문 자체의 문제의식이 약해졌을 가능성이 있습니다."}
    if selected:
        return {"action": "rewrite", "selected": selected, "reason": "선택된 지시만으로 마지막 문장을 교정합니다."}
    return {"action": "keep", "selected": [], "reason": "탐색 기준 이상의 신호가 없습니다."}


def post_review_status(plan: dict, *, body_changed: bool = False, scores: dict[str, Any] | None = None, high: float = .75) -> str:
    """Never treat unresolved factual/role/constraint signals as a reviewable pass."""
    action = plan.get("action")
    if action in ("review_facts", "review_constraints") or any(_signal((scores or {}).get(key, 0)) >= high for key in ("stance", "emotion_flattened", "easy_resolution", "interest_gap")):
        return "hold"
    if body_changed:
        return "review_body_rewrite"
    if action in ("rewrite", "revise_body"):
        return "review_correction"
    if action == "keep":
        return "review_draft"
    raise ValueError("알 수 없는 교정 후 분기입니다.")


def model_json(prompt: str, material: dict, client: httpx.Client, *, tokens: int = 2800) -> dict:
    payload = {"model": AI_MODEL, "input": [{"role": "system", "content": prompt},
               {"role": "user", "content": json.dumps(material, ensure_ascii=False)}], "max_output_tokens": tokens, "store": False}
    attempts = []
    for attempt in range(2):
        started = time.perf_counter()
        try:
            response = client.post(f"{AI_BASE_URL}/responses", json=payload, timeout=120)
            response.raise_for_status()
            data = response.json()
            answer = json.loads(_response_text(data))
            if not isinstance(answer, dict):
                raise ValueError("응답이 JSON 객체가 아닙니다.")
            attempts.append({"status": "ok", "usage": data.get("usage", {}), "seconds": round(time.perf_counter()-started, 3)})
            return {"answer": answer, "model": data.get("model", AI_MODEL), "attempts": attempts}
        except Exception as exc:
            attempts.append({"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:150]}", "seconds": round(time.perf_counter()-started, 3)})
            if attempt == 0:
                payload["input"].append({"role": "user", "content": "지정한 JSON 객체만 다시 반환하세요."})
    return {"error": "생성 응답을 읽지 못했습니다.", "attempts": attempts}


def generate(case: dict, client: httpx.Client) -> dict:
    material = {k: case.get(k, "") for k in ("title", "scene", "roles", "evidence", "body", "stance", "tone", "recent_endings", "connection_preference", "viewer_comment_samples")}
    response = model_json(GENERATION_PROMPT, material, client)
    if "answer" not in response:
        return response
    answer = response["answer"]
    action = answer.get("action")
    if action not in ("draft", "hold", "revise_body"):
        response["error"] = "생성 action이 올바르지 않습니다."
    elif action == "draft":
        ending = str(answer.get("ending", "")).strip()
        response["errors"] = check_result(case["body"], ending)
        response["comment"] = f"{case['body']} {ending}"
    elif str(answer.get("ending", "")).strip():
        response["error"] = "보류/본문 재작성에서는 마지막 문장이 없어야 합니다."
    return response


def diagnose(case: dict, comment: str, ending: str, recent_endings: list[str], client: httpx.Client) -> dict:
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if not key or os.environ.get("TYPESAFE_BASE_URL", "").rstrip("/") != "https://api.experientiallabs.ai":
        raise RuntimeError("기존 무료 Jev 설정을 확인하지 못했습니다.")
    state = {k: case.get(k, "") for k in ("scene", "roles", "evidence", "body", "stance", "tone", "connection_preference")}
    state["original_body"] = case.get("original_body", case.get("body", ""))
    state.update({"comment": comment, "ending": ending, "recent_endings": recent_endings})
    payload = {"model": "jev-latest:free", "state": state,
               "questions": {name: {"type": "noul", "instructions": text} for name, text in JEV_QUESTIONS.items()}}
    started = time.perf_counter()
    response = client.post("https://api.experientiallabs.ai/v1/systemone", headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=45)
    response.raise_for_status()
    data = response.json()
    scores = {name: _signal(data["answers"][name]["noul"]) for name in JEV_QUESTIONS}
    return {"scores": scores, "model": data.get("model"), "usage": data.get("usage", {}),
            "seconds": round(time.perf_counter() - started, 3)}


def correct(case: dict, original: dict, plan: dict, client: httpx.Client) -> dict:
    if plan["action"] != "rewrite":
        raise ValueError("마지막 문장 교정 분기가 아닙니다.")
    prompt = compile_issues(plan["selected"], base_prompt=CORRECTION_PROMPT)
    material = {k: case.get(k, "") for k in ("scene", "roles", "evidence", "body", "stance", "tone", "connection_preference", "recent_endings")}
    material.update({"comment": original.get("comment", ""), "ending": original.get("ending", ""),
                     "emotional_core": original.get("emotional_core", ""), "bridge_reason": original.get("bridge_reason", "")})
    response = model_json(prompt, material, client)
    response["selected"] = plan["selected"]
    response["compiled_prompt"] = prompt
    if "answer" not in response:
        return response
    answer = response["answer"]
    decision = answer.get("decision")
    if decision not in ("rewrite", "keep", "revise_body", "hold"):
        response["error"] = "교정 decision이 올바르지 않습니다."
    elif decision in ("rewrite", "keep"):
        ending = original["ending"] if decision == "keep" else str(answer.get("ending", "")).strip()
        response["ending"] = ending
        response["comment"] = f"{case['body']} {ending}"
        response["errors"] = check_result(case["body"], ending)
    elif str(answer.get("ending", "")).strip():
        response["error"] = "보류/본문 재작성에서는 마지막 문장이 없어야 합니다."
    return response


def rewrite_body(case: dict, client: httpx.Client) -> dict:
    response = model_json(BODY_PROMPT, {k: case.get(k, "") for k in ("scene", "roles", "evidence", "body", "stance", "tone")}, client)
    if "answer" in response:
        body = str(response["answer"].get("body", "")).strip()
        if not body or any(s in body for s in ("운명원", "사주", "#", "http://", "https://")):
            response["error"] = "본문 조건을 확인할 수 없습니다."
        else:
            response["body"] = body
    return response
