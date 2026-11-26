"""Compile brand-specific editorial instructions from fixed issue identifiers.

This is a PoC. Selection and semantic verification are separate from compilation;
the model is never asked to invent its own repair instruction.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Any

from .drafts import validate_comment

ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "docs/prompts/brand-bridge-correction.md").read_text(encoding="utf-8")

RULES = {
    "emotion_flattened": "원문이 무엇 때문에 서운하거나 화나거나 망설이는지 장면의 구체적 사건을 기준으로 다시 짚는다. 그 감정을 단순한 호기심이나 가벼운 후회로 축소하지 않는다. 원문과 다른 감정이나 당사자의 속마음을 사실처럼 지어내지 않는다.",
    "easy_resolution": "관계의 문제가 쉽게 풀리거나 사주를 보면 마음 편하게 결정할 수 있다고 암시하지 않는다. 원래의 불확실성과 갈등이 남아 있어도 된다. 같은 고민을 알 수 있는지 묻는 열린 질문 자체는 해결을 확정하는 진술이 아니다.",
    "interest_gap": "앞의 구체적인 고민에서 운명원 사주를 살펴볼 이유나 관심이 자연스럽게 이어지게 한다. 브랜드를 억지로 끼워 넣거나 '사주를 봐도 소용없다'고 끝내지 않는다. 근거 없는 효과 없이 두 조건을 만족할 수 없으면 hold다.",
    "role_error": "누가 어떤 순서로 행동했고 누가 반응했는지 제공된 역할과 사건 순서 정보를 따른다. 화자를 혼동하거나 서로의 행동과 순서를 뒤바꾸지 않는다. 역할을 확인할 수 없다면 hold다.",
    "abrupt": "앞의 구체적인 장면과 반응의 감정에서 이어지는 생각으로 마지막 문장을 고친다. 그 장면의 구체적 고민을 운명원 사주로 알 수 있는지 묻는 열린 질문, 아쉬움이나 바람을 문맥에 따라 쓸 수 있다. 질문형을 모든 댓글에 강요하지 않는다. 빈말이나 억지 인과를 추가하지 않는다. 자연스럽게 이어지지 않으면 hold다.",
    "advertorial": "상담 안내문처럼 들리는 추상적인 표현이나 긴 설명을 줄인다. 본문의 말투에 맞는 짧은 일상어를 우선하며 기질, 관점, 짚어본다 같은 말을 공식처럼 반복하지 않는다.",
    "unsupported": "사주나 운명원이 구체적인 성향을 분석하거나 갈등을 해결한다는 근거 없는 기능과 효과를 단정하지 않는다. 본인의 실제 이용이나 추천 경험도 만들어내지 않는다.",
    "stance": "분노, 조언, 재회 반대 등의 입장을 유지한다. 잘못한 행동을 운이나 타고난 성향 탓으로 가볍게 넘기거나 재회를 권하는 방향으로 바꾸지 않는다. 실제로 묻고 확인하는 일을 먼저 하자는 의견을 사주부터 보자는 뜻으로 뒤집지 않는다. 보존할 수 없으면 hold다.",
    "repetition": "궁금하네요 같은 상투적인 끝맺음을 기계적으로 되풀이하지 않는다. 같은 뜻의 낱말만 바꾸지 말고 장면의 감정에 맞는 아쉬움, 바람, 가정, 응원, 조언이나 구체적인 질문처럼 표현 기능을 바꿔 본다. 공통으로 브랜드를 언급하거나 질문을 쓴다는 이유만으로 문제라고 취급하지 않는다. 실제 이용 경험이나 효과를 지어내지 않는다.",
}


def compile_issues(selected: list[str], *, base_prompt: str | None = None) -> str:
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(RULES):
        raise ValueError("알려진 교정 항목을 중복 없이 하나 이상 선택해야 합니다.")
    chosen = set(selected)
    return (BASE if base_prompt is None else base_prompt) + "\n\n선택된 교정 항목:\n" + "\n".join(f"- {instruction}" for key, instruction in RULES.items() if key in chosen)


def select_from_signals(signals: dict[str, Any], threshold: float = 0.35) -> list[str]:
    """Exploratory routing; the threshold is not a calibrated pass boundary."""
    mapping = {"emotion_flattened": "emotion_flattened", "easy_resolution": "easy_resolution", "interest_gap": "interest_gap", "role_error": "role_error", "timeline_error": "role_error", "jump": "abrupt", "claim": "unsupported", "stance": "stance", "advertorial": "advertorial", "repetition": "repetition"}
    if not 0 <= threshold <= 1 or set(signals) - set(mapping):
        raise ValueError("유효한 Jev 신호와 0~1 기준을 입력하세요.")
    flags = set()
    for key, value in signals.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Jev 신호는 0~1 사이의 숫자여야 합니다.")
        if value >= threshold:
            flags.add(mapping[key])
    return [key for key in RULES if key in flags]


def check_result(body: str, ending: str) -> list[str]:
    """Check observable constraints only; this cannot establish meaning or truth."""
    body, ending = body.strip(), ending.strip()
    combined = f"{body} {ending}".strip()
    errors = validate_comment(combined)
    if "운명원" in body or "사주" in body:
        errors.append("본문에 브랜드 또는 사주가 들어 있습니다.")
    if ending.count("운명원") != 1 or "사주" not in ending:
        errors.append("마지막 문장에는 운명원 한 번과 사주가 필요합니다.")
    if len(re.split(r"(?<=[.!?。！？])\s+", ending)) > 1:
        errors.append("마지막 문장은 하나여야 합니다.")
    return errors
