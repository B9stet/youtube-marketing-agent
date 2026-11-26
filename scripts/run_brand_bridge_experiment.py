"""Compare brand-closing drafts on fixed video-grounded reaction bodies.

Research only: this script neither controls YouTube nor posts comments.
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
from poc.config import AI_BASE_URL, AI_MODEL
from poc.drafts import _response_text, character_count
from poc.brand_bridge_compiler import check_result

PROMPT = (ROOT / "docs/prompts/brand-bridge-generation.md").read_text(encoding="utf-8")
RUN = ROOT / ".agent/brand-bridge-research" / datetime.now().strftime("run-%Y%m%d-%H%M%S")
BASELINE = "운명원 연애 사주도 궁금해지네요."

# Bodies adapt the earlier PoC or the previously reviewed examples and remain
# identical across all three conditions. Scene descriptions are short, manually
# checked excerpts; they are not claims of automatic highlight selection.
CASES = [
    {
        "id": "reunion", "title": "끝이 보이는 연애", "url": "https://www.youtube.com/watch?v=C2LVY7NZeFU&t=720s",
        "scene": "재회를 고민하는 두 사람이 다시 만나면 행복할지, 이전과 같은 일이 반복될지 이야기한다.",
        "evidence": "우리가 다시 만나면 행복할까?", "emotion": "망설임과 공감, 재회를 확정하거나 반대하지 않음",
        "body": "다시 만나면 행복할까 고민하는 장면이 현실적이네요. 같은 이유로 또 헤어질까 봐 망설이는 마음이 이해돼요.",
        "viewer_pattern": "재회를 응원하면서도 같은 이유로 또 헤어질 수 있다고 걱정하는 반응도 있음.",
    },
    {
        "id": "chemistry", "title": "72시간 소개팅 타이완 편", "url": "https://www.youtube.com/watch?v=IOz2MKthMJA&t=2462s",
        "scene": "더운 날 거울을 보고 늙어 보인다고 하자 다른 출연자가 똑같이 예쁘다고 말하며 대화를 이어 간다.",
        "evidence": "똑같이 예쁘다고", "emotion": "가벼운 호감과 즐거움",
        "body": "더워서 늙어 보인다고 하니까 똑같이 예쁘다고 받아주는 거 귀엽네요ㅋㅋ 둘이 티키타카가 좋다.",
        "viewer_pattern": "두 출연자의 호흡이 잘 맞는다는 반응과 짧은 반말 감상도 있음.",
    },
    {
        "id": "argument", "title": "개같이 싸워도 화해하는 법", "url": "https://www.youtube.com/watch?v=XbPld5XoD7Y&t=60s",
        "scene": "A는 B가 자신과의 약속을 어겨 화났다고 말한다. B는 과거 A가 지키지 않은 약속을 꺼낸다.",
        "evidence": "네가 나랑 한 약속을 어겨서 화가 난 거야", "emotion": "현재 문제를 먼저 인정하라는 조언",
        "body": "약속을 어겼다고 화났는데 거기서 옛날얘기를 꺼내면 대화가 풀리기 어렵죠. 먼저 사과하는 게 맞다고 봐요.",
        "viewer_pattern": "다투는 두 사람의 대화를 보고 답답함과 관계에 대한 생각을 말하는 반응이 있음.",
    },
    {
        "id": "hobby", "title": "소원고민상담소", "url": "https://www.youtube.com/watch?v=K7LsfyRXtX0&t=175s",
        "scene": "사연자는 덕질에 대한 남자친구의 시선 때문에 고민하고, 상담자는 자신이 무엇을 할 때 행복한지도 보라고 조언한다.",
        "evidence": "덕질은 덕질대로", "emotion": "자기 행복을 지키라는 조언에 공감",
        "body": "덕질은 덕질대로 즐기면서 내가 뭘 할 때 행복한지 보라는 말 좋네요. 취향 때문에 스스로를 숨기지 않았으면 해요.",
        "viewer_pattern": "관계 속에서 자신의 취향을 존중받는 문제를 각자 다르게 해석하는 반응이 있음.",
    },
    {
        "id": "birthday_anger", "title": "연애할 때 비참해지는 순간", "url": "https://www.youtube.com/watch?v=b7_QhneQL9E&t=305s",
        "scene": "생일에 연인에게 축하 연락을 받지 못했고, 밤 11시가 넘어 전화가 왔다고 말한다.",
        "evidence": "생일 축하한다는 연락도 없었어요", "emotion": "상대 행동에 화가 난 비판",
        "body": "생일 축하 연락도 없다가 밤 11시 넘어서야 전화하는 건 진짜 너무했다. 저러면 서운할 수밖에 없음.",
        "viewer_pattern": "실망과 외로움, 상대 행동에 대한 비판을 표현하는 반응이 있음.",
    },
]


def candidate(body: str, ending: str) -> dict:
    ending = str(ending).strip()
    comment = f"{body} {ending}" if ending else body
    errors = check_result(body, ending)
    return {"ending": ending, "comment": comment, "characters": character_count(comment), "errors": errors}


def generate(case: dict) -> dict:
    started = time.perf_counter()
    payload = {"model": AI_MODEL, "input": [{"role": "system", "content": PROMPT},
        {"role": "user", "content": json.dumps(case, ensure_ascii=False)}], "max_output_tokens": 3000, "store": False}
    result = {"case": case, "baseline": candidate(case["body"], BASELINE), "attempts": []}
    with httpx.Client(timeout=120) as client:
        for attempt in range(2):
            try:
                resp = client.post(f"{AI_BASE_URL}/responses", json=payload)
                resp.raise_for_status()
                data = resp.json()
                text = _response_text(data)
                parsed = json.loads(text)
                candidates = parsed["candidates"]
                fit = parsed.get("fit", "unknown")
                if fit not in ("good", "conditional", "hold") or not isinstance(candidates, list) or (fit == "hold" and candidates) or (fit != "hold" and not candidates):
                    raise ValueError("candidates must be empty for hold and nonempty otherwise")
                result["attempts"].append({"usage": data.get("usage", {}), "status": "completed"})
                result["fit"] = fit
                result["fit_reason"] = parsed.get("reason", "")
                result["anchor"] = parsed.get("anchor", "")
                result["candidates"] = [{"approach": str(c.get("approach", "")),
                    **candidate(case["body"], c.get("ending", ""))} for c in candidates[:2]]
                result["model"] = data.get("model", AI_MODEL)
                break
            except Exception as exc:
                result["attempts"].append({"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                if attempt == 0:
                    payload["input"].append({"role": "user", "content": "JSON 형식과 두 후보를 지켜 다시 반환하세요."})
    result["seconds"] = round(time.perf_counter() - started, 2)
    return result


def main() -> None:
    RUN.mkdir(parents=True, exist_ok=False)
    (RUN / "cases.json").write_text(json.dumps(CASES, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUN / "generation-prompt.md").write_text(PROMPT, encoding="utf-8")
    print(f"run: {RUN}", flush=True)
    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(generate, case): case["id"] for case in CASES}
        for future in as_completed(tasks):
            key = tasks[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            (RUN / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{key}: {results[key].get('fit', 'ERROR')} / {len(results[key].get('candidates', []))} 후보", flush=True)
    print(f"done: {len(results)} cases", flush=True)


if __name__ == "__main__":
    main()
