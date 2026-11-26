"""Integrated, review-only comment generation and deterministic repair rules.

The routines operate on supplied scene and viewer-reference data. They do not
open a browser, send a comment, or change an external account.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
from typing import Any

import httpx

from .drafts import _response_text, validate_comment

ROOT = Path(__file__).resolve().parents[1]
GENERATION_PROMPT = (ROOT / "docs/prompts/natural-comment-generation.md").read_text(encoding="utf-8")
CORRECTION_PROMPT = (ROOT / "docs/prompts/natural-comment-correction.md").read_text(encoding="utf-8")

# Jev outputs are exploratory signals rather than calibrated probabilities or a
# substitute for a reader's judgment. The compiler never asks a model to invent
# an instruction from those numbers.
JEV_QUESTIONS = {
    "scene_error": "댓글이 제공된 자막의 화자, 실제 발언, 사건 순서를 명백히 바꾸거나 없는 장면을 사실로 말하는가? 참고 댓글의 의견은 영상 사실이 아니다.",
    "stance_drift": "댓글이 선택된 시청자의 응원, 비판, 조언, 걱정이나 거절 존중의 입장을 뒤집는가? 한 장면에 여러 가능한 입장이 있다는 이유로 판단하지 않는다.",
    "emotion_flattened": "댓글이 제공된 장면과 선택한 반응의 구체적인 감정을 지나치게 가벼운 호기심이나 쉬운 해결로 축소하는가?",
    "brand_jump": "운명원에 대한 말이 앞의 구체적인 생각과 감정에 이어지지 않고 갑자기 붙은 것처럼 읽히는가? 브랜드가 등장했다는 이유만으로 판단하지 않는다.",
    "advertorial": "댓글이 장면에 대한 개인의 반응보다 상담 안내나 광고 문구처럼 읽히는가? 직접적인 구어체나 사주에 대한 관심 자체는 문제가 아니다.",
    "repetition": "제공된 비교 시안이 두 개 이상 있을 때 그중 적어도 두 개와 생각과 문장 구조까지 비슷하게 반복하는가? 브랜드명이나 물음표가 같다는 이유만으로 판단하지 않는다. 비교 자료가 두 개 미만이면 아니오다.",
    "tone_mismatch": "댓글의 말높임, 감탄과 리듬이 지정된 시청자 관점이나 참고 말투와 명백히 어긋나 부자연스러운가? 가상 시청자의 1인칭 연애 경험이나 구어체 자체는 문제가 아니다.",
    "false_brand_review": "화자가 운명원을 직접 이용한 경험이나 개인 후기를 사실로 말하는가? '잘 본다던데', '봤으면' 또는 '봐볼까'처럼 간접적으로 듣거나 가정하는 표현은 직접 이용 후기가 아니다.",
}

RULES = {
    "stance_drift": "원래 시청자의 입장과 감정을 유지하고, 비판이나 망설임을 억지 공감이나 재회 권유로 바꾸지 않는다.",
    "emotion_flattened": "장면의 구체적인 고민과 감정의 무게를 보존한다. 어려운 상황을 가벼운 불편이나 쉽게 해결되는 문제로 바꾸지 않는다.",
    "brand_jump": "앞의 장면과 시청자의 생각에서 이어지는 말로 운명원 부분을 바꾼다. 연결이 되지 않으면 본문을 포함해 필요한 범위를 다시 살피고, 억지로 붙이지 않는다.",
    "advertorial": "안내문 같은 추상적인 문구를 줄이고 참고 댓글의 말높임과 일상적인 리듬을 살린다. 사주에 대한 직접적인 관심 자체는 지우지 않는다.",
    "repetition": "다른 시안과 같은 생각과 문장 구조를 반복한다면 장면에 맞는 의견, 질문, 아쉬움, 장난 또는 혼잣말로 기능을 바꾼다. 어미만 기계적으로 바꾸지 않는다.",
    "tone_mismatch": "지정된 시청자의 말투와 감정을 댓글 전체에 맞춘다. 가상 연애 경험이나 구어체 자체를 삭제할 이유로 삼지 않는다.",
}


def _signal(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Jev 신호는 0~1 사이의 숫자여야 합니다.")
    return float(value)


def validate_integrated(comment: str) -> list[str]:
    """Check observable constraints for a complete comment, not its quality."""
    errors = validate_comment(comment)
    sentences = re.split(r"(?<=[.!?。！？])\s+", comment.strip())
    tail = " ".join(sentences[-2:])
    if "운명원" not in tail or "사주" not in tail:
        errors.append("끝부분에 운명원과 연애 사주에 대한 언급을 확인해 주세요.")
    return errors


def compile_feedback(selected: list[str]) -> str:
    """Compile the same ordered instructions for the same issue set."""
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(RULES):
        raise ValueError("알려진 교정 항목을 중복 없이 선택해 주세요.")
    chosen = set(selected)
    return CORRECTION_PROMPT + "\n\n선택된 교정 항목:\n" + "\n".join(f"- {value}" for key, value in RULES.items() if key in chosen)


def route_signals(scores: dict[str, Any], errors: list[str] | None = None, threshold: float = .35, rewrite_threshold: float = .75) -> dict:
    if not 0 <= threshold < rewrite_threshold <= 1 or set(scores) != set(JEV_QUESTIONS):
        raise ValueError("모든 Jev 신호와 유효한 검토/교정 기준이 필요합니다.")
    checked = {k: _signal(v) for k, v in scores.items()}
    if errors:
        return {"action": "review_constraints", "selected": [], "reason": "형식 조건을 확인해야 합니다."}
    if checked["scene_error"] >= threshold or checked["false_brand_review"] >= threshold:
        return {"action": "review_facts", "selected": [], "reason": "장면 또는 직접 이용 후기 여부를 확인해야 합니다."}
    selected = [key for key in RULES if checked[key] >= rewrite_threshold]
    review_signals = [key for key in RULES if threshold <= checked[key] < rewrite_threshold]
    action = "rewrite" if selected else "review_signal" if review_signals else "review_draft"
    reason = ("높은 신호의 고정 규칙을 적용합니다." if selected else
              "애매한 신호가 있어 직접 확인합니다." if review_signals else "신호가 낮아도 문맥을 확인합니다.")
    return {"action": action, "selected": selected, "review_signals": review_signals, "reason": reason}


def model_json(prompt: str, material: dict, client: httpx.Client, *, base_url: str, model: str) -> dict:
    payload = {"model": model, "input": [{"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(material, ensure_ascii=False)}], "max_output_tokens": 6500, "store": False}
    attempts = []
    for _ in range(3):
        try:
            response = client.post(f"{base_url.rstrip('/')}/responses", json=payload, timeout=160)
            response.raise_for_status()
            data = response.json()
            answer = json.loads(_response_text(data))
            if not isinstance(answer, dict):
                raise ValueError("응답이 JSON 객체가 아닙니다.")
            attempts.append({"status": "ok"})
            return {"answer": answer, "model": data.get("model", model), "usage": data.get("usage", {}), "attempts": attempts}
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            attempts.append({"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:150]}"})
            payload["input"].append({"role": "user", "content": "지정한 JSON 객체만 반환하세요."})
    return {"error": "응답을 읽지 못했습니다.", "attempts": attempts}


def generate(case: dict, client: httpx.Client, *, base_url: str, model: str) -> dict:
    material = {key: case.get(key, "") for key in ("video_title", "scene", "scene_quote", "stance", "persona", "voice", "reference_comments", "closing_preference", "feedback")}
    result = model_json(GENERATION_PROMPT, material, client, base_url=base_url, model=model)
    answer = result.get("answer", {})
    if answer.get("action") not in ("draft", "hold") or not isinstance(answer.get("comment"), str):
        result["error"] = result.get("error", "생성 응답의 형식을 확인해 주세요.")
    elif answer["action"] == "draft":
        result["comment"] = answer["comment"].strip()
        result["errors"] = validate_integrated(result["comment"])
    return result


def diagnose(case: dict, comment: str, other_comments: list[str], client: httpx.Client) -> dict:
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if not key or os.environ.get("TYPESAFE_BASE_URL", "").rstrip("/") != "https://api.experientiallabs.ai":
        raise RuntimeError("기존 Jev 설정을 확인하지 못했습니다.")
    state = {name: case.get(name, "") for name in ("scene", "scene_quote", "stance", "persona", "voice", "reference_comments")}
    state.update({"comment": comment, "other_comments": other_comments})
    payload = {"model": "jev-latest:free", "state": state,
        "questions": {name: {"type": "noul", "instructions": question} for name, question in JEV_QUESTIONS.items()}}
    response = client.post("https://api.experientiallabs.ai/v1/systemone", headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    return {"scores": {name: _signal(data["answers"][name]["noul"]) for name in JEV_QUESTIONS},
        "model": data.get("model"), "usage": data.get("usage", {})}


def correct(case: dict, comment: str, selected: list[str], other_comments: list[str], client: httpx.Client, *, base_url: str, model: str) -> dict:
    material = {key: case.get(key, "") for key in ("scene", "scene_quote", "stance", "persona", "voice", "reference_comments")}
    material.update({"comment": comment, "other_comments": other_comments})
    result = model_json(compile_feedback(selected), material, client, base_url=base_url, model=model)
    answer = result.get("answer", {})
    if answer.get("decision") not in ("keep", "rewrite", "hold") or not isinstance(answer.get("comment"), str):
        result["error"] = result.get("error", "교정 응답의 형식을 확인해 주세요.")
    elif answer["decision"] != "hold":
        result["comment"] = comment if answer["decision"] == "keep" else answer["comment"].strip()
        result["errors"] = validate_integrated(result["comment"])
    return result
