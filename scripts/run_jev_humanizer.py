"""Run the Jev/editor-compiler PoC on saved, publicly available video data.

No browser input or posting is performed. Viewer controls are never edited.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poc.config import AI_BASE_URL, AI_MODEL
from poc.drafts import SYSTEM, _response_text, comment_mentions_evidence, validate_comment
from poc.jev_humanizer import ALL_RULES, diagnose, rewrite
from poc.natural_variants import apply_variant

DATA = ROOT / ".agent" / "selection-test" / "results-v2"
CONTEXTS = ROOT / ".agent" / "selection-test" / "contexts"
WORK = ROOT / ".agent" / "jev-humanizer-poc"
REPORT = ROOT / "output" / "jev-humanizer-poc-2026-11-24.md"
METHODS = ("baseline", "lexical", "semantic", "full_llm")
CONTROL_INDEXES = {
    "2E8yLx0VeOE": 2,
    "C2LVY7NZeFU": 5,
    "EKAuoWFfn-s": 3,
    "JqW9PxxvuAA": 1,
    "S66BBYQiSv0": 0,
}


def json_read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def json_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def serializable_cases(cases: list[dict]) -> list[dict]:
    return [{k: v for k, v in case.items() if k != "old_calls"} for case in cases]


def require_same_cases(run_dir: Path, cases: list[dict]) -> None:
    if json_read(run_dir / "cases.json") != serializable_cases(cases):
        raise ValueError("원본 댓글 또는 영상 자료가 바뀌었습니다. 기존 진단을 재사용하지 말고 새로 실행해 주세요.")


def replace_report_section(content: str, marker: str, section: str) -> str:
    """Replace one level-two section while preserving later experiment sections."""
    start = content.find(marker)
    if start < 0:
        return content.rstrip() + "\n\n" + section.strip() + "\n"
    end = content.find("\n## ", start + len(marker))
    suffix = content[end:] if end >= 0 else ""
    return content[:start].rstrip() + "\n\n" + section.strip() + "\n" + suffix


def prepare_existing_jev() -> None:
    """Use existing local configuration without displaying or storing the key."""
    if os.environ.get("EXPERIENTIAL_API_KEY") or os.environ.get("EXPLABS_API_KEY"):
        os.environ["TYPESAFE_API_KEY"] = os.environ.get("EXPERIENTIAL_API_KEY") or os.environ["EXPLABS_API_KEY"]
        os.environ["TYPESAFE_BASE_URL"] = "https://api.experientiallabs.ai"
        os.environ["TYPESAFE_DEFAULT_MODEL"] = "jev-latest:free"
        return
    if os.environ.get("TYPESAFE_API_KEY"):
        return
    helper = Path(os.environ.get("POC_JEV_HELPER_ROOT", r"C:\.workspace\Projects\typesafe-computer-use-windows"))
    source = helper / "typesafe_computer_use" / "credentials.py"
    if not source.is_file():
        raise RuntimeError("기존 Jev 설정을 찾지 못했습니다. TYPESAFE_API_KEY를 설정해 주세요.")
    spec = importlib.util.spec_from_file_location("local_jev_credentials", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("기존 Jev 설정을 읽지 못했습니다.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.prepare_provider()
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise RuntimeError("기존 Jev 인증을 읽지 못했습니다.")


def norm(text: str) -> str:
    return re.sub(r"[^가-힣A-Za-z0-9]", "", unicodedata.normalize("NFKC", str(text))).lower()


def load_context(video_id: str) -> dict:
    paths = (CONTEXTS / f"{video_id}.json", ROOT / ".agent" / "evidence" / f"{video_id}.json")
    for path in paths:
        if path.is_file():
            return json_read(path)
    raise FileNotFoundError(f"저장된 영상 자료 없음: {video_id}")


def find_scene(context: dict, quote: str) -> tuple[str, str]:
    """Return only a locally verifiable quote and neighboring source text."""
    target = norm(quote)
    if len(target) < 4:
        return "", "quote_missing"
    title = context.get("video", {}).get("title", "")
    if target in norm(title):
        return title, "title"
    description = context.get("description", "")
    if target in norm(description):
        return description[:800], "description"
    segments = context.get("captions", {}).get("segments", [])
    for index in range(len(segments)):
        for count in range(1, min(6, len(segments) - index) + 1):
            joined = " ".join(str(s.get("text", "")) for s in segments[index:index + count])
            if target in norm(joined):
                excerpt = segments[max(0, index - 1):min(len(segments), index + count + 2)]
                return " ".join(str(s.get("text", "")) for s in excerpt)[:1000], "captions"
    return "", "quote_not_found"


def collect_cases() -> list[dict]:
    cases = []
    for video_id in CONTROL_INDEXES:
        context = load_context(video_id)
        for method in METHODS:
            path = DATA / f"{video_id}_{method}.json"
            saved = json_read(path)
            generation = saved.get("generation") or {}
            draft = generation.get("draft") or {}
            if not draft.get("comment"):
                raise ValueError(f"저장된 댓글 초안 없음: {path.name}")
            quote = draft.get("evidence_quote", "")
            scene, source = find_scene(context, quote)
            cases.append({
                "case_id": f"{video_id}_{method}", "kind": "generated", "video_id": video_id,
                "title": saved.get("title", ""), "method": method,
                "comment": draft["comment"], "evidence_quote": quote, "scene": scene,
                "scene_source": source, "earlier_grounded": generation.get("grounded"),
                "original_errors": validate_comment(draft["comment"]),
                "old_calls": generation.get("calls", []),
            })
        saved = json_read(DATA / f"{video_id}_baseline.json")
        text = " ".join(saved["input_comments"][CONTROL_INDEXES[video_id]]["text"].split())
        cases.append({"case_id": f"{video_id}_viewer", "kind": "viewer_control", "video_id": video_id,
                      "title": saved.get("title", ""), "comment": text,
                      "evidence_quote": "", "scene": "", "scene_source": "not_evaluated"})
    # Deliberately contrived cases for checking whether the selected categories
    # can fire at all; these are not presented as actual viewer comments.
    cases.extend([
        {"case_id": "fixture_formal", "kind": "synthetic_control", "video_id": "C2LVY7NZeFU",
         "title": "끝이 보이는 연애", "comment": "다시 만나면 행복할까라는 질문은 관계의 불확실성 측면에서 중요한 의미를 지닌다고 사료됩니다.",
         "evidence_quote": "", "scene": "", "expected": ["formal"]},
        {"case_id": "fixture_repetition", "kind": "synthetic_control", "video_id": "C2LVY7NZeFU",
         "title": "끝이 보이는 연애", "comment": "다시 만나면 행복할까 고민하는 장면이 너무 공감돼요. 정말 깊이 공감되고 너무 공감이 되네요.",
         "evidence_quote": "", "scene": "", "expected": ["cliche_empathy", "wordy"]},
    ])
    return cases


def outcome_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def compare_generation(video_id: str, client: httpx.Client) -> dict:
    """One new generation per video, with style guidance in the original call."""
    context = load_context(video_id)
    segments = context.get("captions", {}).get("segments", [])
    if len(segments) > 300:
        segments = [segments[round(i * (len(segments) - 1) / 299)] for i in range(300)]
    material = {"title": context["video"]["title"], "description": context.get("description", "")[:4000],
                "caption_state": context.get("captions", {}).get("state"), "captions": segments,
                "popular_comments": context.get("comments", [])[:10]}
    style = ("\n말투 추가 기준: 구체적인 영상 장면과 반응을 살린다. 보고서식 표현, 같은 뜻의 반복, "
             "상투적인 공감의 반복과 어색한 번역투를 피한다. 감정이나 의견을 획일적으로 순화하지 않는다.")
    payload = {"model": AI_MODEL, "input": [{"role": "system", "content": SYSTEM + style},
               {"role": "user", "content": json.dumps(material, ensure_ascii=False)}],
               "max_output_tokens": 3000, "store": False}
    started = time.perf_counter()
    response = client.post(f"{AI_BASE_URL}/responses", json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    draft = json.loads(_response_text(data))
    comment = str(draft.get("comment", "")).strip()
    quote = str(draft.get("evidence_quote", "")).strip()
    scene, scene_source = find_scene(context, quote)
    errors = validate_comment(comment)
    if not scene:
        errors.append("근거 발언을 저장된 영상 자료에서 찾지 못했습니다.")
    if quote and not comment_mentions_evidence(comment, quote):
        errors.append("댓글에 영상 발언의 핵심 표현이 없습니다.")
    if "운명원" in comment or "사주" in comment:
        errors.append("공감 초안에 브랜드 또는 사주가 들어갔습니다.")
    return {"comment": comment, "evidence_quote": quote, "scene_source": scene_source, "errors": errors,
            "model": AI_MODEL, "usage": data.get("usage", {}),
            "seconds": round(time.perf_counter() - started, 3)}


def print_progress(message: str) -> None:
    print(message, flush=True)


def short_error_list(errors: list[str]) -> str:
    return " / ".join(errors) if errors else "통과"


def display_scores(result: dict) -> str:
    if "error" in result:
        return "오류: " + result["error"]
    return ", ".join(f"{ALL_RULES[k].label} {v:.2f}" for k, v in result["scores"].items())


def render_report(cases: list[dict], results: dict, generations: dict, started_at: str) -> str:
    generated = [x for x in cases if x["kind"] == "generated"]
    viewer = [x for x in cases if x["kind"] == "viewer_control"]
    fixtures = [x for x in cases if x["kind"] == "synthetic_control"]
    successful = [results[c["case_id"]] for c in cases if "error" not in results.get(c["case_id"], {"error": "missing"})]
    actions: dict[str, int] = {}
    for case in generated:
        action = results.get(case["case_id"], {}).get("action", "diagnosis_error")
        actions[action] = actions.get(action, 0) + 1
    rewrites = [results[c["case_id"]].get("rewrite", {}) for c in generated]
    completed = [r for r in rewrites if r and "error" not in r]
    jev_tokens = sum(int((r.get("usage") or {}).get("input_tokens") or 0) for r in successful)
    jev_costs = [(r.get("usage") or {}).get("cost") for r in successful]
    llm_tokens = sum(int((r.get("usage") or {}).get("input_tokens") or 0) for r in completed)
    fixture_rewrites = [results.get(c["case_id"], {}).get("rewrite", {}) for c in fixtures]
    fixture_tokens = sum(int((r.get("usage") or {}).get("input_tokens") or 0) for r in fixture_rewrites)
    gen_tokens = sum(int((r.get("usage") or {}).get("input_tokens") or 0) for r in generations.values())
    lines = ["# Jev 진단과 교정 지시 조합 PoC", "", f"실행: {started_at}", "",
             "## 시험 방법", "",
             f"- 저장된 초안 {len(generated)}개: 영상 5편 × 이전에 생성한 4가지 방식. 실제 시청자 댓글 {len(viewer)}개는 진단만 했고, 의도적으로 어색하게 만든 점검 문장 {len(fixtures)}개도 시험했다.",
             "- 각 댓글의 Jev 질문은 한 요청으로 묶었다. 발언이 저장 자료에서 확인된 초안만 해당 장면의 전후 자막을 보냈다. 시청자 댓글은 말투만 검사했다.",
             "- 임시 기준: 문제 확률 0.75 이상이면 해당 규칙 선택, 0.35 이상 0.75 미만이면 애매한 신호로 기록. 명백한 맥락 모순이 0.75 이상이면 수정 대신 검토로 보낸다.",
             "- LLM은 선택된 정해진 문장 규칙과 원문, 발언, 인접 자막만 받아 최대 한 번 수정했다. 같은 진단값을 같은 규칙에 넣으면 지시문은 동일하다.",
             "- 길이, 링크, 해시태그, 브랜드와 기존 근거 표현을 프로그램으로 검사했다. 의미와 감정 보존, 자연스러움은 사람이 별도로 읽고 판단해야 한다.",
             "- 비교군은 기존 PoC와 같은 영상 자료를 사용하되 생성 지침에 말투 기준을 추가해 영상당 한 번씩 새로 생성했다. 생성 시점과 선택 장면은 다를 수 있다.",
             "- 자동 판정값은 품질 점수가 아니다. 테스트용 수치와 소규모 표본으로 실제 정확도나 최적 기준을 확정하지 않았다.", "",
             "## 실행 결과", "",
             f"- Jev 진단 완료: {len(successful)}/{len(cases)}건. 저장된 초안의 분기: {json.dumps(actions, ensure_ascii=False)}.",
             f"- 저장된 초안의 LLM 수정 응답: {len(completed)}건. 형식 검사 통과 {sum(not r.get('errors') for r in completed)}건, 검사 실패 {sum(bool(r.get('errors')) for r in completed)}건. 별도 점검 문장 수정은 아래에 표기한다.",
             f"- Jev가 보고한 입력 토큰: {jev_tokens:,}개. 비용 필드 합계: {sum(x for x in jev_costs if isinstance(x, (int,float))):.6f} (통화는 공급자 응답에 별도 명시되지 않음).",
             f"- 저장된 초안의 추가 수정 LLM 입력 토큰: {llm_tokens:,}개. 별도 점검 문장 수정: {fixture_tokens:,}개. 말투 지침을 포함한 새 생성 비교군 입력 토큰: {gen_tokens:,}개. 기존 초안 생성에 사용한 과거 토큰은 이 합계에서 제외했다.",
             "- 소수의 제어 사례를 포함한 실험이므로 사람이 더 자연스럽다고 선호하는지, 비용 대비 이득이 있는지 확인하는 자료로 사용한다.", "",
             "## 기존 PoC 초안과 수정 결과", ""]
    labels = {"keep": "원문 유지", "review_uncertain": "판단 애매, 원문 유지", "review_conflict": "장면 충돌 의심, 검토", "rewrite": "교정 시도"}
    for case in generated:
        r = results.get(case["case_id"], {"error": "missing"})
        lines += [f"### {case['title']} / {case['method']}", "", f"- 원문: {case['comment']}",
                  f"- 근거: {case['evidence_quote']} ({case['scene_source']})", f"- 진단: {display_scores(r)}"]
        if "error" not in r:
            lines.append(f"- 분기: {labels.get(r['action'], r['action'])}")
            rr = r.get("rewrite")
            if rr and "error" not in rr:
                lines.append(f"- 교정: {rr['comment']}")
                lines.append(f"- 기계적 검사: {short_error_list(rr.get('errors', []))}")
            elif rr:
                lines.append(f"- 교정 오류: {rr['error']}")
            variant = r.get("casual_variant")
            if variant and variant.get("applied"):
                lines.append(f"- 구어체 표기 후보 ({variant['label']}): {variant['comment']}")
        lines.append("")
    lines += ["## 생성 단계에 말투 기준을 추가한 비교군", ""]
    for case in [x for x in generated if x["method"] == "baseline"]:
        g = generations.get(case["video_id"], {"error": "missing"})
        lines += [f"### {case['title']}", "", f"- 기존 PoC: {case['comment']}"]
        if "error" in g:
            lines.append(f"- 새 생성 오류: {g['error']}")
        else:
            lines += [f"- 말투 기준 추가 후 새 생성: {g['comment']}",
                      f"- 새 생성 근거: {g['evidence_quote']}", f"- 기계적 검사: {short_error_list(g.get('errors', []))}"]
        lines.append("")
    lines += ["## 실제 시청자 댓글 진단", "", "기존에 수집된 댓글 일부다. 자연스러움의 정답으로 확정한 표본은 아니며, Jev가 어떤 신호를 보내는지 비교하기 위해 사용했다.", ""]
    for case in viewer:
        r = results.get(case["case_id"], {"error": "missing"})
        lines += [f"- **{case['title']}**: {case['comment']}", f"  - {display_scores(r)}", ""]
    lines += ["## 의도적으로 만든 점검 문장", "", "아래 문장은 실제 댓글이 아니라 진단 기능 확인을 위해 만든 문장이다.", ""]
    for case in fixtures:
        r = results.get(case["case_id"], {"error": "missing"})
        lines += [f"- {case['comment']}", f"  - 예상 항목: {', '.join(ALL_RULES[k].label for k in case['expected'])}", f"  - 실제 진단: {display_scores(r)}"]
        rr = r.get("rewrite")
        if rr and "error" not in rr:
            lines += [f"  - 교정: {rr['comment']}", f"  - 기계적 검사: {short_error_list(rr.get('errors', []))}"]
        elif rr:
            lines.append(f"  - 교정 오류: {rr['error']}")
        lines.append("")
    lines += ["## 저장 위치", "", "- 개별 진단, 응답, 조립된 지시문과 사용량: `.agent/jev-humanizer-poc/` 아래 실행별 폴더.",
              "- 이 시험은 댓글 초안 생성과 수정까지만 수행한다.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnosis-only", action="store_true", help="Jev 진단만 실행")
    parser.add_argument("--resume-run", type=Path, help="기존 실행 폴더에서 미완료 건 이어서 수행")
    parser.add_argument("--pace-seconds", type=float, default=8.0, help="연속된 Jev 요청 사이 대기 시간")
    parser.add_argument("--casual-rate", type=float, default=0.30, help="가벼운 구어체 표기 변형을 시도할 비율")
    args = parser.parse_args()
    if args.pace_seconds < 0 or args.pace_seconds > 60:
        raise ValueError("Jev 요청 간격은 0~60초여야 합니다.")
    if not 0 <= args.casual_rate <= 1:
        raise ValueError("표기 변형 비율은 0~1이어야 합니다.")
    prepare_existing_jev()
    cases = collect_cases()
    if args.resume_run:
        run_dir = args.resume_run.resolve()
        if not run_dir.is_relative_to(WORK.resolve()) or not (run_dir / "cases.json").is_file():
            raise ValueError("기존 PoC 실행 폴더를 지정해야 합니다.")
        require_same_cases(run_dir, cases)
    else:
        run_dir = WORK / datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=False)
        # Exclude prior full LLM replies and request material from the dataset copy.
        json_write(run_dir / "cases.json", serializable_cases(cases))
    results: dict[str, dict] = {}
    generations: dict[str, dict] = {}
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for case in cases:
        path = run_dir / "diagnosis" / f"{case['case_id']}.json"
        if path.is_file():
            saved = json_read(path)
            if "error" not in saved:
                results[case["case_id"]] = saved
        path = run_dir / "rewrites" / f"{case['case_id']}.json"
        if path.is_file() and case["case_id"] in results:
            results[case["case_id"]]["rewrite"] = json_read(path)
    for video_id in CONTROL_INDEXES:
        path = run_dir / "generation" / f"{video_id}.json"
        if path.is_file():
            saved = json_read(path)
            if "error" not in saved:
                generations[video_id] = saved
    with httpx.Client() as client:
        pending = [c for c in cases if c["case_id"] not in results]
        pending.sort(key=lambda c: (0 if c["kind"] == "synthetic_control" else 1, c["case_id"]))
        for index, case in enumerate(pending, 1):
            try:
                results[case["case_id"]] = diagnose(case, client=client)
            except Exception as exc:
                results[case["case_id"]] = {"error": outcome_error(exc)}
            json_write(run_dir / "diagnosis" / f"{case['case_id']}.json", results[case["case_id"]])
            print_progress(f"진단 {index}/{len(pending)}: {case['case_id']} {results[case['case_id']].get('action', results[case['case_id']].get('error'))}")
            if results[case["case_id"]].get("error") == "HTTP 429":
                print_progress("Jev 요청 제한을 만나 추가 요청을 중단합니다. 기존 결과는 보존됩니다.")
                break
            if index < len(pending) and args.pace_seconds:
                time.sleep(args.pace_seconds)
        if not args.diagnosis_only:
            eligible = [c for c in cases if c["kind"] in ("generated", "synthetic_control")
                        and results.get(c["case_id"], {}).get("action") == "rewrite"
                        and "rewrite" not in results[c["case_id"]]]
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(rewrite, c, results[c["case_id"]], client=client): c for c in eligible}
                for index, future in enumerate(as_completed(futures), 1):
                    case = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"error": outcome_error(exc)}
                    results[case["case_id"]]["rewrite"] = result
                    json_write(run_dir / "rewrites" / f"{case['case_id']}.json", result)
                    print_progress(f"교정 {index}/{len(eligible)}: {case['case_id']} {result.get('error', '완료')}")
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(compare_generation, vid, client): vid for vid in CONTROL_INDEXES if vid not in generations}
                for index, future in enumerate(as_completed(futures), 1):
                    video_id = futures[future]
                    try:
                        generations[video_id] = future.result()
                    except Exception as exc:
                        generations[video_id] = {"error": outcome_error(exc)}
                    json_write(run_dir / "generation" / f"{video_id}.json", generations[video_id])
                    print_progress(f"생성 비교 {index}/{len(CONTROL_INDEXES)}: {video_id} {generations[video_id].get('error', '완료')}")
            for case in cases:
                result = results.get(case["case_id"], {})
                if case["kind"] != "generated" or result.get("action") not in ("keep", "review_uncertain", "rewrite"):
                    continue
                edit = result.get("rewrite")
                if result["action"] == "rewrite" and (not edit or edit.get("error") or edit.get("errors")):
                    continue
                text = edit["comment"] if edit else case["comment"]
                result["casual_variant"] = apply_variant(text, evidence_quote=case.get("evidence_quote", ""),
                                                         case_id=case["case_id"], rate=args.casual_rate)
    json_write(run_dir / "results.json", {"diagnoses": results, "generation_comparison": generations})
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render_report(cases, results, generations, started_at), encoding="utf-8")
    print_progress(f"보고서: {REPORT}")
    print_progress(f"상세 기록: {run_dir}")
    return 0 if len(results) == len(cases) and all("error" not in r for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
