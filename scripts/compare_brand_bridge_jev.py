"""Blindly compare fixed-body brand closings with Jev, reversing presentation order."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_jev_humanizer import prepare_existing_jev

RUNS = ROOT / ".agent/brand-bridge-research"


def ask(client: httpx.Client, case: dict, first: str, second: str) -> dict:
    state = {"scene": case["scene"], "reaction_body": case["body"], "comment_A": first, "comment_B": second}
    questions = {
        "preference": {"type": "choice", "instructions": "두 댓글 중 영상 반응과 마지막 사주 언급이 더 자연스럽게 이어지고 말투와 입장이 일관된 쪽은? 차이가 불분명하면 tie를 선택한다.",
                       "criteria": {"A": "A가 더 자연스럽다", "B": "B가 더 자연스럽다", "tie": "비슷하거나 둘 다 어색하다"}},
        "jump_A": {"type": "noul", "instructions": "A의 마지막 문장이 영상 반응과 감정상 자연스러운 연결 없이 갑자기 튀는가? 브랜드가 있다는 이유만으로 판단하지 않는다."},
        "jump_B": {"type": "noul", "instructions": "B의 마지막 문장이 영상 반응과 감정상 자연스러운 연결 없이 갑자기 튀는가? 브랜드가 있다는 이유만으로 판단하지 않는다."},
        "claim_A": {"type": "noul", "instructions": "A의 마지막 문장은 사주의 효과, 서비스 기능, 이용 경험 또는 인물의 속마음/미래에 대해 근거 없는 사실을 단정하는가?"},
        "claim_B": {"type": "noul", "instructions": "B의 마지막 문장은 사주의 효과, 서비스 기능, 이용 경험 또는 인물의 속마음/미래에 대해 근거 없는 사실을 단정하는가?"},
        "stance_A": {"type": "noul", "instructions": "A의 마지막 문장이 앞의 감정이나 의견을 뒤집거나 잘못된 행동에 대한 비판을 타고난 성향 탓으로 흐리는가?"},
        "stance_B": {"type": "noul", "instructions": "B의 마지막 문장이 앞의 감정이나 의견을 뒤집거나 잘못된 행동에 대한 비판을 타고난 성향 탓으로 흐리는가?"},
        "advertorial_A": {"type": "noul", "instructions": "A의 마지막 문장에 실제 시청자의 일상 반응보다 상담 안내문이나 광고처럼 들리는 추상적인 설명이나 장황한 표현이 있는가? 단순히 브랜드가 있다는 이유만으로 판단하지 않는다."},
        "advertorial_B": {"type": "noul", "instructions": "B의 마지막 문장에 실제 시청자의 일상 반응보다 상담 안내문이나 광고처럼 들리는 추상적인 설명이나 장황한 표현이 있는가? 단순히 브랜드가 있다는 이유만으로 판단하지 않는다."},
    }
    start = time.perf_counter()
    response = client.post("https://api.experientiallabs.ai/v1/systemone", headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"},
                           json={"model": "jev-latest:free", "state": state, "questions": questions}, timeout=45)
    response.raise_for_status()
    data = response.json()
    return {"answers": data.get("answers", {}), "usage": data.get("usage", {}), "model": data.get("model"),
            "seconds": round(time.perf_counter() - start, 3)}


def main() -> None:
    prepare_existing_jev()
    if os.environ.get("TYPESAFE_BASE_URL", "").rstrip("/") != "https://api.experientiallabs.ai":
        raise RuntimeError("기존 무료 Jev 연결을 확인할 수 없어 중단합니다.")
    run = sorted(RUNS.glob("run-*"))[-1]
    results = json.loads((run / "results.json").read_text(encoding="utf-8"))
    output = run / "jev-pairwise.json"
    comparisons = []
    previous = 0.0
    with httpx.Client() as client:
        for key, record in results.items():
            if not record.get("candidates"):
                continue
            baseline = record["baseline"]["comment"]
            generated = record["candidates"][0]["comment"]
            first_is_baseline = int(hashlib.sha256(key.encode()).hexdigest()[:2], 16) % 2 == 0
            for reversed_order in (False, True):
                is_baseline = first_is_baseline != reversed_order
                a, b = (baseline, generated) if is_baseline else (generated, baseline)
                delay = 8 - (time.monotonic() - previous)
                if delay > 0:
                    time.sleep(delay)
                row = {"id": key, "order": "baseline_A" if is_baseline else "generated_A"}
                try:
                    row.update(ask(client, record["case"], a, b))
                except Exception as exc:
                    row["error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
                    comparisons.append(row)
                    output.write_text(json.dumps(comparisons, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"{key} {row['order']}: ERROR, 중단", flush=True)
                    return
                finally:
                    previous = time.monotonic()
                comparisons.append(row)
                output.write_text(json.dumps(comparisons, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{key} {row['order']}: {row['answers'].get('preference', {}).get('choice')}", flush=True)
    print(f"done: {len(comparisons)} comparisons", flush=True)


if __name__ == "__main__":
    main()
