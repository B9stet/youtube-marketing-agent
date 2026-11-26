"""Generate and review drafts for five newly collected YouTube videos.

Only captions, public viewer comments and local model/Jev calls are used. This
script contains no browser input, posting, or other YouTube mutations.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_contextual_fresh_videos import produce, review
from scripts.run_contextual_brand_poc import RECENT
from poc.ending_diversity import analyze_endings

PLANS = (
    {"id": "8xAfwDQtDj8", "start": 120, "end": 175, "quote": "나 다시 너 만날 생각 없어", "tone_id": "qualified_view", "comments": (1,),
     "roles": "한 사람은 달라졌다고 말하며 다시 만나자고 한다. 상대는 다시 만날 생각이 없다고 분명히 말한다.",
     "stance": "변하고 싶은 마음과 재회를 거절한 상대의 선택을 함께 존중한다. 다시 만난다고 단정하거나 거절을 무시하지 않는다.",
     "connection_preference": {"form": "장면에 맞는 질문, 의견 또는 바람 중 선택", "topic": "다시 만나고 싶은 마음과 거절이 엇갈린 관계"}},
    {"id": "n7ho4C1aKVw", "start": 115, "end": 151, "quote": "그럼 실물은 어때요?", "tone_id": "humorous_reaction", "comments": (),
     "roles": "소개팅 중 한 사람이 실물은 어떤지 묻고 상대는 사진보다 훨씬 낫다고 대답한다.",
     "stance": "칭찬을 주고받는 장면에 대한 가벼운 호감과 재미. 실제 교제나 연애 감정은 단정하지 않는다."},
    {"id": "6Hq96MZqego", "start": 109, "end": 137, "quote": "그게 된다고", "tone_id": "uncertain_worried", "comments": (2,),
     "roles": "한 사람은 상황 때문에 헤어졌고 차단하거나 욕할 필요는 없다며 좋게 끝난 것 같다고 말한다. 대화 상대는 그게 가능하냐고 묻는다.",
     "stance": "이별 뒤 서로를 배려하는 마음과 계속 연락하는 일이 어려울 수 있다는 망설임을 표현한다.",
     "connection_preference": {"form": "장면에 맞는 질문, 의견 또는 바람 중 선택", "topic": "헤어진 뒤 계속 연락하는 것이 서로에게 어떨지"}},
    {"id": "Cr2XlCDrxUM", "start": 115, "end": 178, "quote": "다른 걸 틀렸다고", "tone_id": "scene_interpretation", "comments": (5, 6),
     "roles": "한 출연자가 과거 자신이 다른 사람과 다른 점을 틀렸다고 생각하며 이별을 말했던 일을 돌아본다. 상대의 최종 선택이나 재회 여부는 알 수 없다.",
     "stance": "서로의 차이를 오해했던 점을 돌아보는 말에 반응한다. 재회 여부를 단정하지 않는다.",
     "connection_preference": {"form": "장면에 맞는 질문, 의견 또는 바람 중 선택", "topic": "서로 다른 생각을 이해해도 비슷한 갈등이 반복될지"}},
    {"id": "U7PdMvogEGQ", "start": 50, "end": 102, "quote": "장거리 연애", "tone_id": "question_reaction", "comments": (),
     "roles": "한 출연자는 IT 영업 일과 가끔 국내 출장을 이야기한다. 다른 출연자는 해외 영업과 미국 바이어 이메일 업무를 이야기한다. 이후 한 출연자가 해외 영업 지인의 장기 체류를 예로 들며 장거리 연애 같을 수 있다고 생각한다. 해외 영업을 하는 상대방의 실제 출장 빈도나 장기 체류는 이 구간에서 확인되지 않았다.",
     "stance": "서로의 생활 방식이 관계에 어떤 영향을 줄지 조심스럽게 궁금해한다. 상대의 출장 빈도와 실제 장거리 연애 여부는 단정하지 않는다."},
)


def seconds(timestamp: str) -> float:
    pieces = timestamp.split(":")
    if len(pieces) != 3:
        raise ValueError("자막 시간 형식이 올바르지 않습니다.")
    return int(pieces[0]) * 3600 + int(pieces[1]) * 60 + float(pieces[2])


def window(segments: list[dict], start: float, end: float, evidence: str) -> list[dict]:
    if start > end:
        raise ValueError("구간의 시작은 끝보다 늦을 수 없습니다.")
    selected = [s for s in segments if start <= seconds(s["start"]) <= end]
    selected.sort(key=lambda segment: seconds(segment["start"]))
    compact = lambda text: re.sub(r"\s+", "", str(text))
    if not selected or compact(evidence) not in compact(" ".join(s["text"] for s in selected)):
        raise ValueError("선택한 자막 구간에서 근거 발언을 찾지 못했습니다.")
    return selected


def build_cases(run: Path) -> list[dict]:
    cases = []
    for plan in PLANS:
        path = run / "contexts" / f"{plan['id']}.json"
        context = json.loads(path.read_text(encoding="utf-8"))
        if context.get("captions", {}).get("state") != "available":
            raise ValueError(f"{plan['id']}에 자막이 없습니다.")
        segments = window(context["captions"]["segments"], plan["start"], plan["end"], plan["quote"])
        comments = [context["comments"][index]["text"][:180] for index in plan["comments"] if index < len(context.get("comments", []))]
        video = context["video"]
        case = {"id": plan["id"], "title": video["title"], "url": f"{video['url']}&t={plan['start']}s",
                "scene": "\n".join(f"[{s['start']}] {s['text']}" for s in segments),
                "roles": plan["roles"], "evidence": plan["quote"], "stance": plan["stance"],
                "tone_id": plan["tone_id"], "viewer_comment_samples": comments, "recent_endings": RECENT,
                "caption_source": context["captions"]["source"], "window_start": plan["start"], "window_end": plan["end"],
                "segments_used": len(segments), "segments_total": len(context["captions"]["segments"]),
                "comment_sample_count": len(comments)}
        if "connection_preference" in plan:
            case["connection_preference"] = plan["connection_preference"]
        cases.append(case)
    return cases


def ending_after_review(record: dict) -> str:
    correction = record.get("correction", {})
    if correction.get("answer", {}).get("decision") == "rewrite" and correction.get("ending"):
        return str(correction["ending"])
    regeneration = record.get("regeneration", {}).get("answer", {})
    if regeneration.get("action") == "draft" and regeneration.get("ending"):
        return str(regeneration["ending"])
    return str(record.get("initial", {}).get("ending", ""))


def main() -> None:
    run = sorted((ROOT / ".agent/fresh-video-poc").glob("run-*"))[-1]
    cases = build_cases(run)
    catalog = json.loads((ROOT / "poc/data/comment_tones.json").read_text(encoding="utf-8"))
    tones = {tone["id"]: tone for tone in catalog["groups"]}
    records = []
    (run / "selected-cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"사용할 영상 {len(cases)}편, 저장 위치 {run}", flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(produce, case, tones[case["tone_id"]]): case for case in cases}
        for future in as_completed(tasks):
            case = tasks[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {"case": case, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
            records.append(record)
            (run / "fresh-generation.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            print("생성", case["id"], "draft" if record.get("initial") else "hold", flush=True)
    review(target_run=run, use_batch_context=True)
    reviewed_path = run / "fresh-reviewed.json"
    if reviewed_path.exists():
        reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
        endings = [ending_after_review(item) for item in reviewed if item.get("initial")]
        analysis = analyze_endings(endings)
        analysis["scope"] = "최초 일괄 시안의 문장 형태 점검; 사실 검토 통과를 의미하지 않음"
        (run / "batch-diversity.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        print("일괄 반복 점검", json.dumps(analysis, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
