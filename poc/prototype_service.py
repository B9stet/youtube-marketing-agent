"""Build grounded scene candidates and integrated, reviewable comment drafts."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
import re
import unicodedata
from typing import Callable

from .drafts import character_count, comment_mentions_evidence, evidence_is_grounded
from .natural_comment import GENERATION_PROMPT, compile_feedback, route_signals, validate_integrated
from .prototype_providers import ProviderError, diagnose, generate_json

KEYWORDS = ("마음", "고민", "걱정", "불안", "행복", "헤어", "갈등", "다시", "사랑", "연애", "미안", "상처", "싸우", "관계", "좋아", "혼자", "기다리", "서운", "이별", "외로", "재회", "결혼", "부담", "썸")
RELATIONSHIP_CUES = ("마음에", "마음이", "좋아해", "좋아한다", "사귀", "재회", "헤어", "이별", "고백", "소개팅", "연애", "썸", "결혼", "상대", "사랑", "감정", "갈등", "서운", "걱정", "불안")
COMMENT_CUES = ("연애", "마음", "상대", "소개팅", "재회", "이별", "현실", "관계", "설레", "공감", "답답", "아쉽", "다시", "남자", "여자", "선택", "사랑")
CREDIT_COMMENT = re.compile(r"(?:instagram|인스타그램|장소\s*협찬|제작\s*지원|출연진\s*[:：]|문의\s*[:：]|business\s*inquir)", re.I)
PERSPECTIVES = {
    "reference": {"label": "참고 댓글 관점", "stance": "장면에 관련된 참고 댓글이 있으면 그 입장과 감정을 이어받고, 관련이 없으면 영상 장면 자체에 반응한다.", "persona": "영상에 직접 반응하는 가상 시청자"},
    "immersed": {"label": "장면에 이입", "stance": "등장인물의 대화나 선택에 이입해 솔직하게 반응한다.", "persona": "드라마를 보듯 장면에 이입하는 가상 시청자"},
    "experience": {"label": "경험에 비춰보기", "stance": "장면과 맞는 지난 연애의 감정을 떠올려 자신의 관점에서 말한다.", "persona": "가상의 짧은 연애 기억을 떠올리는 시청자"},
    "opinion": {"label": "의견과 조언", "stance": "장면 속 행동에 대한 구체적인 생각이나 조언을 말한다.", "persona": "자신의 의견을 솔직하게 말하는 가상 시청자"},
}


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", str(text or ""))
    return re.sub(r"\s+", " ", re.sub(r"https?://\S+", " ", text)).strip()


def _seconds(value: str) -> float:
    try:
        parts = str(value).replace(",", ".").split(":")
        return sum(float(part) * (60 ** i) for i, part in enumerate(reversed(parts)))
    except (TypeError, ValueError):
        return 0.0


def _clock(seconds: float) -> str:
    minutes, second = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{second:02d}" if hours else f"{minutes:02d}:{second:02d}"


def _grams(text: str) -> Counter:
    compact = re.sub(r"[^가-힣A-Za-z0-9]", "", _clean(text).lower())
    return Counter(compact[i:i + 3] for i in range(max(0, len(compact) - 2)))


def _similarity(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    denom = math.sqrt(sum(n*n for n in a.values()) * sum(n*n for n in b.values()))
    return sum(n * b.get(term, 0) for term, n in a.items()) / denom if denom else 0.0


def _comments(context: dict) -> list[dict]:
    seen, items = set(), []
    for item in context.get("comments", [])[:20]:
        text = _clean(item.get("text", ""))[:500]
        key = re.sub(r"\W", "", text)
        if len(key) < 4 or key in seen or CREDIT_COMMENT.search(text):
            continue
        seen.add(key)
        items.append({"text": text, "likes": item.get("likes") or 0,
                      "url": context.get("video", {}).get("url", ""), "kind": "이 영상의 인기 댓글"})
    return items


def _rank_comments(comments: list[dict], text: str, title: str = "") -> list[dict]:
    grams = _grams(text)
    title_grams = _grams(title)
    ranked = []
    for item in comments:
        comment_grams = _grams(item["text"])
        scene_score = _similarity(grams, comment_grams)
        title_score = _similarity(title_grams, comment_grams)
        cues = min(sum(word in item["text"] for word in COMMENT_CUES), 3)
        score = scene_score + .25 * title_score + .005 * cues
        ranked.append((score, {**item, "scene_relevance": round(scene_score, 4),
            "kind": "이 영상 댓글" if scene_score >= .04 else "이 영상 인기 댓글 · 장면 관련성 확인 필요"}))
    ranked.sort(key=lambda row: -row[0])
    return [item for _, item in ranked[:3]]


def select_scenes(context: dict, limit: int = 3) -> list[dict]:
    """Rank bounded, overlapping caption windows without feeding the whole transcript to the model."""
    captions = context.get("captions", {})
    comments = _comments(context)
    groups: dict[int, list[dict]] = {}
    for segment in captions.get("segments", [])[:20000]:
        text = _clean(segment.get("text", ""))
        if not text:
            continue
        second = _seconds(segment.get("start", "0"))
        bucket = int(second // 30) * 30
        for start in (bucket, bucket - 30):
            if start >= 0:
                groups.setdefault(start, []).append({"start": str(segment.get("start", "")), "text": text, "seconds": second})
    title_grams = _grams(context.get("video", {}).get("title", ""))
    comment_grams = [_grams(c["text"]) for c in comments]
    ranked = []
    for start, segments in groups.items():
        window = " ".join(s["text"] for s in segments)
        if len(re.findall(r"[가-힣]", window)) < 8:
            continue
        grams = _grams(window[:5000])
        similarities = sorted((_similarity(grams, c) for c in comment_grams), reverse=True)[:3]
        reactions = sum(similarities) / len(similarities) if similarities else 0.0
        relationship = min(sum(word in window for word in RELATIONSHIP_CUES) / 4.0, 1.0)
        variety = min(sum(word in window for word in KEYWORDS) / 6.0, 1.0)
        # Repetition of a generic word such as "좋아" must not drown out a
        # different scene that contains an actual relationship concern.
        score = .55 * reactions + .15 * _similarity(grams, title_grams) + .22 * relationship + .08 * variety
        for comment in comments:
            times = re.findall(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", comment["text"])
            if any(int(sec) < 60 and start <= int(minute)*60 + int(sec) < start + 60 for minute, sec in times):
                score += .10
                break
        ranked.append((score, start, segments, window))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    chosen = []
    for score, start, segments, window in ranked:
        if any(abs(start - scene["start"]) < 60 for scene in chosen):
            continue
        usable = [x for x in segments if len(re.sub(r"\s", "", x["text"])) >= 4]
        if not usable:
            continue
        selected = max(usable, key=lambda x: (sum(word in x["text"] for word in KEYWORDS), min(len(x["text"]), 70)))
        quote = selected["text"][:120]
        scene_text = "\n".join(f"{item['start']} {item['text']}" for item in segments)[:3500]
        chosen.append({"id": str(start), "start": start, "end": start + 60,
            "time": f"{_clock(start)} ~ {_clock(start + 60)}", "quote": quote,
            "context": scene_text, "score": round(score, 4), "source": captions.get("source", "caption"),
            "comments": _rank_comments(comments, window, context.get("video", {}).get("title", "")), "manual": False})
        if len(chosen) >= limit:
            break
    if chosen:
        return chosen
    return [{"id": "no-caption", "start": 0, "end": 0, "time": "제목과 설명",
        "quote": "", "context": _clean(context.get("description", ""))[:1500], "source": captions.get("state", "absent"),
        "comments": [{**item, "kind": "이 영상 인기 댓글 · 장면 관련성 확인 필요"} for item in comments[:3]], "manual": False}]


def manual_scene(context: dict, quote: str) -> dict:
    quote = _clean(quote)
    if not 4 <= len(quote) <= 500:
        raise ValueError("직접 확인한 장면이나 발언을 4~500자로 입력해 주세요.")
    return {"id": "manual", "start": 0, "end": 0, "time": "직접 확인한 장면",
        "quote": quote, "context": quote, "source": "manual",
        "comments": _rank_comments(_comments(context), quote, context.get("video", {}).get("title", "")), "manual": True}


def build_case(context: dict, scene: dict, perspective: str, previous: list[str],
               tone: str = "natural", spelling: bool = False, reference_index: int | None = None) -> dict:
    if perspective not in PERSPECTIVES:
        raise ValueError("댓글 관점을 확인해 주세요.")
    choice = PERSPECTIVES[perspective]
    voice = "참고 댓글이 해당 장면과 관련되는지 확인하고, 관련될 때 관점과 어조를 참고하되 개인사와 문장을 복사하지 않는다."
    if tone == "casual":
        voice += " 평소 댓글을 쓰듯 조금 더 편한 구어체를 사용한다."
    if spelling:
        voice += " 읽기 쉬운 가벼운 띄어쓰기 변형이나 오타를 어울릴 때 한 곳 정도 허용한다."
    references = scene.get("comments", [])[:3]
    if reference_index is not None:
        if reference_index < 0 or reference_index >= len(references):
            raise ValueError("선택한 참고 댓글을 찾지 못했습니다.")
        references = [references[reference_index]]
    return {"video_title": context["video"]["title"], "scene": scene["context"], "scene_quote": scene["quote"],
        "stance": choice["stance"], "persona": choice["persona"],
        "voice": voice,
        "reference_comments": references,
        "closing_preference": "장면의 감정과 생각에서 운명원 연애 사주에 대한 관심으로 자연스럽게 이어간다. 같은 질문이나 어미를 반복하지 않는다.",
        "other_comments": previous[-4:]}


def _answer(result: dict) -> tuple[str, str, str]:
    answer = result.get("answer", {})
    if answer.get("action") not in ("draft", "hold"):
        raise ProviderError("댓글 생성 응답의 형식을 확인해 주세요.")
    return str(answer.get("action")), _clean(answer.get("comment", "")), _clean(answer.get("concern", ""))


def generate_initial_draft(context: dict, scene: dict, perspective: str, settings: dict,
                           previous: list[str], stage: Callable[[str], None] = lambda _: None,
                           tone: str = "natural", spelling: bool = False, reference_index: int | None = None,
                           publish: Callable[[str, dict], None] = lambda _phase, _value: None) -> dict:
    """Generate one grounded draft without waiting for its independent review."""
    case = build_case(context, scene, perspective, previous, tone, spelling, reference_index)
    material = dict(case)
    operations = []
    def record(kind: str, value: dict, provider: str) -> None:
        operations.append({"step": kind, "provider": provider, "model": value.get("model"),
            "usage": value.get("usage", {}), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    prompt = GENERATION_PROMPT + (
        '\n추가로 concern 필드에 제공된 장면의 연애 고민을 한 줄로 쓴다. '
        '참고 댓글 중 이 장면과 생각이 이어지는 것만 골라 reference_index에 0부터 시작하는 번호를 넣고, '
        '이어받은 관점을 reference_viewpoint에 짧게 적는다. 적절한 참고 댓글이 없으면 reference_index는 null, reference_viewpoint는 빈 문자열이다. '
        '응답: {"action":"draft|hold","comment":"완성 댓글","reason":"보류 사유 또는 빈 문자열","concern":"고민 한 줄","reference_index":0,"reference_viewpoint":"참고한 관점"}.'
    )
    stage("generating")
    result = generate_json(settings, prompt, material)
    record("generate", result, settings.get("generation_provider", "local"))
    action, original, concern = _answer(result)
    if action == "hold":
        held = {"action": "hold", "comment": "", "concern": concern,
            "reason": _clean(result["answer"].get("reason", "자연스러운 연결을 확인해 주세요.")), "model": result.get("model"),
            "review": {"action": "hold", "reason": "자연스러운 댓글을 만들지 못했습니다."}, "operations": operations}
        publish("generated", held)
        return held
    errors = validate_integrated(original)
    if errors:
        material["feedback"] = "이전 초안의 조건 오류를 고쳐 주세요: " + "; ".join(errors)
        result = generate_json(settings, prompt, material)
        record("generate_retry", result, settings.get("generation_provider", "local"))
        action, original, concern = _answer(result)
        errors = validate_integrated(original) if action == "draft" else ["생성을 보류했습니다."]
    if action == "hold" or errors:
        held = {"action": "hold", "comment": original, "concern": concern, "errors": errors,
            "reason": "; ".join(errors), "model": result.get("model"),
            "review": {"action": "review_constraints", "reason": "댓글 조건을 확인해 주세요."}, "operations": operations}
        publish("generated", held)
        return held

    selection = result.get("answer", {}).get("reference_index")
    references = case["reference_comments"]
    if isinstance(selection, bool) or not isinstance(selection, int) or not 0 <= selection < len(references):
        selection = None
    final_reference_index = (reference_index if reference_index is not None else selection) if selection is not None else None
    reference_viewpoint = _clean(result.get("answer", {}).get("reference_viewpoint", "")) if selection is not None else ""
    case["reference_comments"] = [references[selection]] if selection is not None else []
    grounded = bool(scene.get("quote")) and (scene.get("manual") or evidence_is_grounded(scene["quote"], context))
    mentions = bool(scene.get("quote")) and comment_mentions_evidence(original, scene["quote"])
    initial = {"action": "draft", "comment": original, "original": original, "concern": concern,
        "character_count": character_count(original), "errors": [], "grounded": grounded,
        "mentions_evidence": mentions, "review": {"action": "review_pending", "reason": "Jev로 초안을 검토하고 있어요."},
        "diagnosis": None, "diagnosis_text": None, "corrected": False, "model": result.get("model"),
        "usage": result.get("usage", {}), "operations": list(operations), "perspective": perspective,
        "reference_index": final_reference_index, "requested_reference_index": reference_index,
        "reference_viewpoint": reference_viewpoint, "case": case}
    publish("generated", initial)
    return initial


def review_generated_draft(initial: dict, scene: dict, settings: dict, previous: list[str],
                           stage: Callable[[str], None] = lambda _: None,
                           publish: Callable[[str, dict], None] = lambda _phase, _value: None) -> dict:
    """Diagnose one exact source text and optionally suggest one correction."""
    original, case = initial["comment"], initial["case"]
    grounded, mentions = initial["grounded"], initial["mentions_evidence"]
    operations = list(initial.get("operations", []))
    def record(kind: str, value: dict, provider: str) -> None:
        operations.append({"step": kind, "provider": provider, "model": value.get("model"),
            "usage": value.get("usage", {}), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    final, final_diagnosis, correction = original, None, None
    review = {"action": "review_facts" if not grounded else "review_draft", "reason": "영상 장면을 확인해 주세요."}
    original_review, original_diagnosis = None, None
    try:
        stage("diagnosing")
        before = diagnose(settings, case, original, previous[-4:])
        record("diagnose", before, settings.get("jev_provider", "existing"))
        review = route_signals(before["scores"])
        if not grounded:
            review = {"action": "review_facts", "reason": "영상 속 구체적인 장면을 직접 확인해 주세요."}
        original_review, original_diagnosis = dict(review), before
        publish("diagnosed", {"diagnosis": before, "diagnosis_text": original,
                               "review": dict(review), "operations": list(operations)})
        final_diagnosis = before
        if grounded and review["action"] == "rewrite":
            stage("correcting")
            correction = generate_json(settings, compile_feedback(review["selected"]),
                {**case, "comment": original, "other_comments": previous[-4:]})
            record("correct", correction, settings.get("generation_provider", "local"))
            answer = correction.get("answer", {})
            revised = _clean(answer.get("comment", ""))
            if answer.get("decision") == "rewrite" and revised and not validate_integrated(revised):
                stage("verifying")
                after = diagnose(settings, case, revised, previous[-4:])
                record("verify", after, settings.get("jev_provider", "existing"))
                final, final_diagnosis = revised, after
                review = route_signals(after["scores"])
                mentions = bool(scene.get("quote")) and comment_mentions_evidence(final, scene["quote"])
            elif answer.get("decision") == "hold":
                review = {"action": "hold", "reason": _clean(answer.get("reason", "교정 시안을 보류했습니다."))}
            elif answer.get("decision") != "keep":
                review = {"action": "review_constraints", "reason": "교정안의 형식을 확인해 원문을 유지했습니다."}
    except ProviderError as exc:
        review = {"action": "review_unavailable", "reason": str(exc)}
        operations.append({"step": "review_error", "message": str(exc), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if original_diagnosis is None:
            original_review = dict(review)
    if review.get("action") == "rewrite":
        review = {**review, "reason": "교정 후에도 높은 검토 신호가 남아 있어 직접 확인해 주세요."}
    if not grounded:
        review = {"action": "review_facts", "reason": "영상 속 구체적인 장면을 직접 확인해 주세요."}
    return {**initial, "action": "draft", "comment": final, "original": original,
        "character_count": character_count(final), "errors": validate_integrated(final), "grounded": grounded,
        "mentions_evidence": mentions, "review": review, "diagnosis": final_diagnosis,
        "corrected": final != original, "operations": operations,
        "original_review": original_review, "original_diagnosis": original_diagnosis}


def create_draft(context: dict, scene: dict, perspective: str, settings: dict,
                 previous: list[str], stage: Callable[[str], None] = lambda _: None,
                 tone: str = "natural", spelling: bool = False, reference_index: int | None = None,
                 publish: Callable[[str, dict], None] = lambda _phase, _value: None) -> dict:
    """Compatibility wrapper for callers that want the full review in one call."""
    initial = generate_initial_draft(context, scene, perspective, settings, previous, stage,
                                     tone, spelling, reference_index, publish)
    if initial.get("action") != "draft":
        return initial
    return review_generated_draft(initial, scene, settings, previous, stage, publish)
