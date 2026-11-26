"""Curate traceable tone patterns from already collected viewer comments."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_jev_humanizer import json_read, json_write

VIDEO_DIR = ROOT / ".agent" / "comment-research-2026-11-24" / "videos"
CATALOG = ROOT / "poc" / "data" / "comment_tones.json"
REPORT = ROOT / "output" / f"comment-tone-collection-{datetime.now():%Y-%m-%d}.md"


def example(video_id: str, comment_id: str, excerpt: str) -> dict:
    return {"video_id": video_id, "comment_id": comment_id, "excerpt": excerpt}


# Hand-selected, short original excerpts. Selection is intentionally diverse;
# these examples are not a representative frequency estimate or generation copy.
GROUPS = [
    {"id": "brief_informal", "label": "짧고 단정적인 반말", "register": "반말/문장 생략",
     "features": "상황을 먼저 짚고 '~임', '~음'처럼 짧게 판단을 마친다.",
     "caution": "짧다는 이유로 영상 장면이나 의견까지 생략하지 않는다.",
     "examples": [
         example("IOz2MKthMJA", "UgwsAQyHNTfCZNjdTph4AaABAg", "둘이 천생연분인 증거임."),
         example("C2LVY7NZeFU", "Ugwd5WcBaW6uNe2I0Yh4AaABAg", "나이먹음 고쳐지기 쉽지않음"),
     ]},
    {"id": "warm_polite", "label": "차분한 해요체 공감", "register": "해요체",
     "features": "자신의 느낌이나 상대의 마음을 담담하게 말하고 '~네요', '~것 같아요'로 마친다.",
     "caution": "서로 다른 감정을 모두 '깊이 공감해요'로 바꾸지 않는다.",
     "examples": [
         example("JqW9PxxvuAA", "Ugwv8WNxrPBA5ZgF-Ph4AaABAg", "저시절 생각이 나는 영상이라 공감이 많이 되네요."),
         example("ZtoxofTKy7I", "UgwqagQm4ncJYwhs_TR4AaABAg", "서로의 언어를 이해하는 시간을 가졌으면 좋겠네요."),
     ]},
    {"id": "question_reaction", "label": "질문하듯 바로 반응", "register": "반말 또는 해요체",
     "features": "행동을 짚고 '왜 그러지?', '~거임?' 같은 물음으로 답답함이나 궁금함을 드러낸다.",
     "caution": "질문을 쓴다는 이유로 답을 이미 아는 사실까지 의문으로 바꾸지 않는다.",
     "examples": [
         example("JqW9PxxvuAA", "Ugw1JKRFyuxCvf6V1k14AaABAg", "저런 멘트 왜 치는거임??"),
         example("ku4sejh-O4s", "UgyS4yPFAfbDWKfeSQB4AaABAg", "왜 다른 사람들 눈치는 잘보면서 여친 눈치만 안봄??"),
     ]},
    {"id": "angry_critical", "label": "화가 난 직설적 비판", "register": "반말/직설",
     "features": "마음에 걸린 행동을 구체적으로 짚고 강한 평가를 바로 붙인다.",
     "caution": "분노나 비판을 자동으로 공손한 공감으로 순화하지 않는다.",
     "examples": [
         example("EKAuoWFfn-s", "UgylAVsNKyRBvnjoYPt4AaABAg", "팀장 개양심없네"),
         example("ra7McPkIfeE", "UgwPO1Ipf17MYgSHHwJ4AaABAg", "짜증나서 못보겠네"),
     ]},
    {"id": "qualified_view", "label": "조건을 붙여 의견 내기", "register": "해요체/혼합",
     "features": "한쪽 가능성을 인정한 뒤 특정 조건이나 이유를 덧붙인다.",
     "caution": "반대 입장과 찬성 입장을 평균적인 중립 의견으로 합치지 않는다.",
     "examples": [
         example("C2LVY7NZeFU", "UgzbQ5gnDvJxBlk_4214AaABAg", "둘 다 애정이 남아있다면 한 번 더"),
         example("Tz3Hagf0WMI", "Ugy1ysE65dvp9t635Gl4AaABAg", "싫은건 아닌데 눈치 없는 설정을 떠나서"),
     ]},
    {"id": "practical_advice", "label": "직접 권하거나 조심스럽게 조언", "register": "해요체/하십시오체",
     "features": "'하지 마세요', '~보시면 좋을 것 같아요'처럼 행동을 권한다.",
     "caution": "조언은 선택한 장면과 관련된 행동에 붙이고 원문에 없는 경험은 만들지 않는다.",
     "examples": [
         example("erMw0-yYWFE", "UgxqdUK0CcidyL8SLWt4AaABAg", "감정 억누르지마세요 언젠가는 터집니다."),
         example("ZtoxofTKy7I", "UgynP9D7RpRcaI6x8_J4AaABAg", "본인문제부터 되돌아 보시면 좋을 것 같아요!!"),
     ]},
    {"id": "uncertain_worried", "label": "망설임이나 걱정을 남기는 말투", "register": "혼합",
     "features": "바라는 마음과 불안함을 함께 두고 문장을 여운 있게 끝낸다.",
     "caution": "불확실한 감상을 확신에 찬 결론으로 바꾸지 않는다.",
     "examples": [
         example("C2LVY7NZeFU", "UgwKmLDRFR3gfoBpdal4AaABAg", "또 헤어질것 같은 두려움"),
         example("JqW9PxxvuAA", "UgxfO5xNu0ocRkNOX3l4AaABAg", "저때 안놀면 언제 노나 싶으면서도"),
     ]},
    {"id": "humorous_reaction", "label": "웃으며 상황을 짚기", "register": "반말/감탄",
     "features": "장면을 짧게 콕 집고 'ㅋㅋ', 생략부호나 의외라는 반응을 섞는다.",
     "caution": "모든 댓글에 ㅋㅋ를 붙이지 않고 영상 장면과 감정에 맞춰 선택한다.",
     "examples": [
         example("Tz3Hagf0WMI", "Ugypl58Nb6MmsLUkMOh4AaABAg", "얘네 음악 동아리였짘ㅋㅋㅋㅋ"),
         example("EKAuoWFfn-s", "UgxIBOnhVGOIYTwqtmN4AaABAg", "풍물동아리 왜케 잘함 ㅋㅋㅋㅋ"),
     ]},
    {"id": "scene_interpretation", "label": "행동을 보고 마음 해석", "register": "해요체/서술",
     "features": "출연자의 구체적인 행동을 짚은 뒤 마음이나 이유를 자신의 해석으로 덧붙인다.",
     "caution": "관찰과 추측을 구분해 영상에 없는 속마음을 사실로 단정하지 않는다.",
     "examples": [
         example("ZtoxofTKy7I", "UgwqagQm4ncJYwhs_TR4AaABAg", "속상하고 걱정돼서 하는 말이지"),
         example("C2LVY7NZeFU", "UgyYea0Le-2q7E3T9Yd4AaABAg", "뭔가 어색하면서도 불편해보이냐"),
     ]},
    {"id": "casual_spelling", "label": "띄어쓰기와 구어식 표기", "register": "반말/해요체",
     "features": "띄어쓰기를 붙이거나 '좋겟', '더라구요'처럼 일상적인 표기를 섞는다.",
     "caution": "표기 변형은 일부에만 적용하고 인용 발언과 브랜드 문장은 보호한다.",
     "examples": [
         example("C2LVY7NZeFU", "UgwtMvVzYAfYDxFwL2d4AaABAg", "데이트햇음좋겟다ㅜㅜ"),
         example("erMw0-yYWFE", "UgzjWmXjHVvyb4Krpa14AaABAg", "제발 그만하자고 하더라구요"),
     ]},
]


def load_comments() -> tuple[dict[tuple[str, str], dict], int]:
    rows = {}
    videos = set()
    for path in sorted(VIDEO_DIR.glob("*.json")):
        video = json_read(path)
        for item in video.get("comments", []):
            text = str(item.get("text", ""))
            if item.get("is_uploader") or len(re.findall(r"[가-힣]", text)) < 4:
                continue
            rows[(video["video_id"], item["id"])] = {"text": text, "title": video.get("title", "")}
            videos.add(video["video_id"])
    return rows, len(videos)


def build_catalog() -> dict:
    rows, video_count = load_comments()
    output_groups = []
    for group in GROUPS:
        examples = []
        for selected in group["examples"]:
            key = (selected["video_id"], selected["comment_id"])
            if key not in rows or selected["excerpt"] not in rows[key]["text"]:
                raise ValueError(f"수집 댓글에서 인용을 확인하지 못했습니다: {key}")
            examples.append({**selected, "title": rows[key]["title"],
                             "url": f"https://www.youtube.com/watch?v={key[0]}&lc={key[1]}"})
        output_groups.append({**group, "examples": examples})
    return {"description": "수집된 유튜브 댓글에서 고른 말투 예시. 실제 표현을 짧게 발췌했다.",
            "observed_comments": len(rows), "observed_videos": video_count,
            "selection": "말투 다양성을 위해 수동 선별한 예시이며 집단별 빈도 통계가 아니다.",
            "usage": ["원문을 그대로 복사하지 않고 말투의 특징만 참고한다.",
                      "영상 근거와 선택한 감정, 입장을 우선한다.",
                      "감정, 존댓말 여부, 문장 구조, 표기 습관은 독립적으로 조합할 수 있다.",
                      "사용자가 제공하지 않은 개인적 경험을 새로 만들지 않는다."],
            "groups": output_groups}


def render(catalog: dict) -> str:
    examples = [example for group in catalog["groups"] for example in group["examples"]]
    lines = ["# 실제 유튜브 댓글에서 수집한 말투", "",
             f"기존에 수집한 영상 {catalog['observed_videos']}편의 시청자 댓글 {catalog['observed_comments']}개를 다시 살펴보고, 서로 다른 말투가 드러나는 짧은 발췌 {len(examples)}개를 골랐다.",
             "유형은 비교를 위해 만든 분류이며 하나의 댓글은 여러 유형에 걸칠 수 있다. 아래 예시는 댓글 전체와 작성자 정보 대신 해당 표현과 출처만 보여준다.", "",
             "## 말투와 감정은 따로 고른다", "",
             "반말은 농담뿐 아니라 걱정이나 비판에도 쓰이고, 존댓말로 강하게 조언하는 댓글도 있다. `ㅋㅋ` 역시 즐거움뿐 아니라 황당함이나 반대 의견과 함께 등장한다. 그래서 '분노=반말', '공감=해요체'처럼 고정하지 않는다.", "",
             "## 수집한 표현", ""]
    for group in catalog["groups"]:
        lines += [f"### {group['label']}", "", f"**말투:** {group['register']}. {group['features']}", ""]
        for selected in group["examples"]:
            lines.append(f"- “{selected['excerpt']}” ([원문 댓글]({selected['url']}))")
        lines += ["", f"**적용할 때:** {group['caution']}", ""]
    lines += ["## 생성에 활용할 기준", "",
              "1. 영상의 구체적인 장면과 반응을 먼저 정한다.",
              "2. 공감, 응원, 조언, 비판, 질문 중 선택한 입장과 감정을 유지한다.",
              "3. 그다음 말높임, 질문형 여부, 문장 길이와 구어체 표기를 고른다. 한 댓글의 특징을 모두 복사하지 않는다.",
              "4. 실제 경험을 말하는 형식은 작성자가 제공한 경험이 있을 때만 참고한다.",
              "5. 띄어쓰기나 오타는 일부 댓글에 적용하되, 인용한 발언과 브랜드 문장은 보호한다.", "",
              "각 발췌와 출처, 특징을 담은 구조화된 자료는 `poc/data/comment_tones.json`에 있다. 이 자료를 댓글 생성에 자동 주입하지는 않았다.", ""]
    return "\n".join(lines)


def main() -> int:
    catalog = build_catalog()
    json_write(CATALOG, catalog)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render(catalog), encoding="utf-8")
    n = sum(len(group["examples"]) for group in catalog["groups"])
    print(f"{catalog['observed_videos']}개 영상, 댓글 {catalog['observed_comments']}개에서 {len(catalog['groups'])}가지 말투, 발췌 {n}개 수집")
    print(f"문서: {REPORT}")
    print(f"자료: {CATALOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
