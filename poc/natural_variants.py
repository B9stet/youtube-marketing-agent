"""Optional, bounded informal Korean variants for generated comment drafts.

The rules are deliberately narrow and derived from the previously collected
viewer comments.  This module makes no network calls and never posts content.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import re
from typing import Any

from .drafts import comment_mentions_evidence, validate_comment


@dataclass(frozen=True)
class Variant:
    key: str
    kind: str
    pattern: str
    replacement: str
    label: str


# A rule may only transform its exact, observed form. These are not a general
# spellchecker and must not invent random keyboard slips or alter quotations.
VARIANTS = (
    Variant("spacing_geot", "spacing", r"것 같", "것같", "띄어쓰기"),
    Variant("spacing_geo", "spacing", r"거 같", "거같", "띄어쓰기"),
    Variant("spacing_su", "spacing", r"(할|될|볼|갈|알) 수", r"\1수", "띄어쓰기"),
    Variant("spelling_geyo", "spelling", r"거예요", "거에요", "맞춤법 변형"),
    Variant("spelling_joget", "spelling", r"좋겠(?=[가-힣])", "좋겟", "오타"),
    Variant("spelling_deoragu", "spelling", r"더라고요", "더라구요", "구어식 표기"),
    Variant("spoken_mam", "colloquial", r"마음(?=(?:이|은|을|에|도|만|\s|$))", "맘", "구어체 축약"),
)

QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|‘[^’\n]*’|“[^”\n]*”")


def _normalized_positions(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text):
        if char.isalnum():
            folded = char.casefold()
            chars.extend(folded)
            positions.extend([index] * len(folded))
    return "".join(chars), positions


def protected_spans(comment: str, evidence_quote: str) -> list[tuple[int, int]]:
    """Preserve explicit quotations, shared evidence phrases and brand sentences."""
    spans = [(m.start(), m.end()) for m in QUOTED.finditer(comment)]
    normalized_quote, _ = _normalized_positions(evidence_quote)
    normalized_comment, positions = _normalized_positions(comment)
    if normalized_quote and positions:
        matches = SequenceMatcher(None, normalized_quote, normalized_comment, autojunk=False).get_matching_blocks()
        for match in matches:
            if match.size >= 4:
                spans.append((positions[match.b], positions[match.b + match.size - 1] + 1))
    # Preserve the entire sentence containing the brand or service term.
    for match in re.finditer(r"운명원|사주", comment):
        before = max(comment.rfind(mark, 0, match.start()) for mark in ".!?。！？") + 1
        afters = [comment.find(mark, match.end()) for mark in ".!?。！？"]
        afters = [index for index in afters if index >= 0]
        spans.append((before, min(afters) + 1 if afters else len(comment)))
    return spans


def _fraction(seed: str, *parts: object) -> float:
    payload = "\x1f".join([seed, *(str(part) for part in parts)]).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") / 2**64


def apply_variant(
    comment: str,
    *,
    evidence_quote: str = "",
    case_id: str = "",
    rate: float = 0.30,
    seed: str = "youtube-casual-poc-v1",
    kinds: tuple[str, ...] = ("spacing", "spelling", "colloquial"),
) -> dict[str, Any]:
    """Apply at most one safe variant, or return the exact original unchanged."""
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
        raise ValueError("표기 변형 비율은 0과 1 사이여야 합니다.")
    if not set(kinds).issubset({"spacing", "spelling", "colloquial"}):
        raise ValueError("알 수 없는 표기 변형 종류입니다.")
    if validate_comment(comment):
        return {"comment": comment, "applied": False, "reason": "invalid_input"}
    identity = case_id or comment
    if _fraction(seed, "gate", identity) >= rate:
        return {"comment": comment, "applied": False, "reason": "rate_skipped"}
    protected = protected_spans(comment, evidence_quote)
    candidates = []
    for variant in VARIANTS:
        if variant.kind not in kinds:
            continue
        for match in re.finditer(variant.pattern, comment):
            if any(match.start() < end and match.end() > start for start, end in protected):
                continue
            replacement = match.expand(variant.replacement)
            candidate = comment[:match.start()] + replacement + comment[match.end():]
            if validate_comment(candidate):
                continue
            if evidence_quote and comment_mentions_evidence(comment, evidence_quote):
                if not comment_mentions_evidence(candidate, evidence_quote):
                    continue
            candidates.append((variant, match, candidate, replacement))
    if not candidates:
        return {"comment": comment, "applied": False, "reason": "no_safe_candidate"}
    variant, match, revised, replacement = min(
        candidates, key=lambda item: _fraction(seed, "choice", identity, item[0].key, item[1].start())
    )
    return {"comment": revised, "applied": True, "reason": "applied", "rule": variant.key,
            "kind": variant.kind, "label": variant.label,
            "before": match.group(0), "after": replacement}
