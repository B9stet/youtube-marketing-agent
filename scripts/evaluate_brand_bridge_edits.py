"""Blindly compare hand-edited closings and the model's one actual rewrite."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.brand_bridge_compiler import check_result
from poc.drafts import character_count
from scripts.compare_brand_bridge_jev import ask
from scripts.run_jev_humanizer import prepare_existing_jev


def main() -> None:
    prepare_existing_jev()
    if os.environ.get("TYPESAFE_BASE_URL", "").rstrip("/") != "https://api.experientiallabs.ai":
        raise RuntimeError("기존 무료 Jev 연결을 확인할 수 없습니다.")
    runs = sorted((ROOT / ".agent/brand-bridge-research").glob("run-*"))
    run = next(p for p in reversed(runs) if (p / "corrections.json").exists())
    records = json.loads((run / "results.json").read_text(encoding="utf-8"))
    edits = json.loads((run / "corrections.json").read_text(encoding="utf-8"))
    hand = {
        "chemistry": "둘 궁합도 운명원에서 사주로 보면 어떨지 궁금하네ㅋㅋ",
        "reunion": "이런 인연은 운명원에서 사주로 어떻게 볼지 궁금하네요.",
    }
    pairs = []
    for key, ending in hand.items():
        body = records[key]["case"]["body"]
        errors = check_result(body, ending)
        if errors:
            raise ValueError(f"{key}: {errors}")
        pairs.append({"id": key, "case": records[key]["case"], "first_name": "model", "second_name": "hand",
                      "first": records[key]["candidates"][0]["comment"], "second": f"{body} {ending}",
                      "characters": character_count(f"{body} {ending}")})
    if edits.get("hobby", {}).get("decision") == "rewrite":
        pairs.append({"id": "hobby", "case": records["hobby"]["case"], "first_name": "model", "second_name": "rewrite",
                      "first": records["hobby"]["candidates"][0]["comment"], "second": edits["hobby"]["comment"]})
    result = {"pairs": pairs, "comparisons": []}
    output = run / "jev-edits.json"
    previous = 0.0
    with httpx.Client() as client:
        for pair in pairs:
            for reverse in (False, True):
                a, b = (pair["second"], pair["first"]) if reverse else (pair["first"], pair["second"])
                delay = 8 - (time.monotonic() - previous)
                if delay > 0:
                    time.sleep(delay)
                row = {"id": pair["id"], "order": "second_A" if reverse else "first_A"}
                try:
                    row.update(ask(client, pair["case"], a, b))
                except Exception as exc:
                    row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                    result["comparisons"].append(row)
                    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"{pair['id']}: ERROR, 중단", flush=True)
                    return
                finally:
                    previous = time.monotonic()
                result["comparisons"].append(row)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{pair['id']} {row['order']}: {row['answers'].get('preference', {}).get('choice')}", flush=True)
    print(f"done: {len(result['comparisons'])} comparisons", flush=True)


if __name__ == "__main__":
    main()
