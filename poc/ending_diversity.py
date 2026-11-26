"""Narrow, deterministic indicators for reviewing a batch of comment endings."""
from __future__ import annotations

import re
from collections import Counter


def ending_style(ending: str) -> dict[str, str | bool]:
    text = re.sub(r"\s+", " ", str(ending).strip())
    question = bool(re.search(r"[?？]", text) or re.search(r"(?:려나|을까|ㄹ까|을까요|나요|편인가|는가|ㄴ가|건가|거임|뭐지)[.!…ㅋㅎ\s]*$", text))
    if re.search(r"어떻게\s*(?:나올|볼|보는|보일|될|될지)", text):
        template = "어떻게_볼지_나올지"
    elif re.search(r"알\s*수\s*있(?:을까|는지|을지)", text):
        template = "알_수_있는지"
    elif re.search(r"궁금(?:하|해|했|해지|하네)", text):
        template = "궁금"
    else:
        template = "기타"
    match = re.match(r"^(운명원\s*(?:연애\s*)?사주(?:라도|도|를|로|가|는|에선)?)", text)
    lead = re.sub(r"\s+", " ", match.group(1)) if match else "기타"
    return {"question": question, "template": template, "lead": lead}


def analyze_endings(endings: list[str]) -> dict:
    styles = [ending_style(ending) for ending in endings if str(ending).strip()]
    question_count = sum(bool(style["question"]) for style in styles)
    templates = Counter(str(style["template"]) for style in styles if style["template"] != "기타")
    leads = Counter(str(style["lead"]) for style in styles if style["lead"] != "기타")
    repeated = {key: count for key, count in templates.items() if count >= 2}
    # These flags request review, not forced style changes or proof of quality.
    return {"count": len(styles), "question_count": question_count, "templates": dict(templates), "repeated_leads": {key: count for key, count in leads.items() if count >= 2},
            "repeated_templates": repeated,
            "question_dominant": len(styles) >= 3 and question_count / len(styles) >= .70}
