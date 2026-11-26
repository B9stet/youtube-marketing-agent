"""Compare focused, situation-specific brand endings for two earlier scenarios."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.brand_bridge_compiler import check_result
from poc.contextual_brand_poc import diagnose, model_json, plan_correction
from scripts.run_contextual_brand_poc import CASES, RECENT
from scripts.run_jev_humanizer import prepare_existing_jev

PROMPT = (ROOT / "docs/prompts/contextual-anchor-retry.md").read_text(encoding="utf-8")
ANCHORS = {"birthday": "상대에게 소중히 여겨지는 관계인지, 이런 외로움 속에서 관계를 계속 이어가야 할지",
           "reunion": "서로에게 마음이 남아 있어도 같은 상처가 반복될까 두려워 다시 시작할지 망설임"}


def make(case: dict) -> dict:
    material = {k: case[k] for k in ("scene", "roles", "evidence", "body", "stance", "tone")}
    material["관계_고민_가설"] = ANCHORS[case["id"]]
    material["최근_반복_예시"] = RECENT
    with httpx.Client(timeout=120) as client:
        response = model_json(PROMPT, material, client)
    record = {"case": case, "anchor_hypothesis": ANCHORS[case["id"]], "generation": response, "candidates": []}
    answer = response.get("answer", {})
    if answer.get("action") == "draft":
        for ending in answer.get("candidates", [])[:2]:
            ending = str(ending).strip()
            record["candidates"].append({"ending": ending, "comment": case["body"] + " " + ending,
                                         "errors": check_result(case["body"], ending)})
    return record


def main() -> None:
    run = sorted((ROOT / ".agent/contextual-bridge-poc").glob("run-*"))[-1]
    cases = [case for case in CASES if case["id"] in ANCHORS]
    (run / "targeted-anchor-prompt.md").write_text(PROMPT, encoding="utf-8")
    output = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(make, case): case["id"] for case in cases}
        for future in as_completed(futures):
            try:
                item = future.result()
            except Exception as exc:
                item = {"case": {"id": futures[future]}, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
            output.append(item)
            (run / "targeted-anchor-results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            print("재생성", futures[future], len(item.get("candidates", [])), flush=True)
    prepare_existing_jev()
    previous = time.monotonic()
    with httpx.Client(timeout=120) as client:
        for item in output:
            case = item["case"]
            for candidate in item.get("candidates", []):
                if candidate["errors"]:
                    continue
                delay = 8 - (time.monotonic() - previous)
                if delay > 0:
                    time.sleep(delay)
                try:
                    candidate["diagnosis"] = diagnose(case, candidate["comment"], candidate["ending"], RECENT, client)
                    candidate["plan"] = plan_correction(candidate["diagnosis"]["scores"], candidate["errors"], body_changed=False)
                except Exception as exc:
                    candidate["diagnosis"] = {"error": f"{type(exc).__name__}: {str(exc)[:180]}"}
                    (run / "targeted-anchor-results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
                    print("Jev 중단", case.get("id"), type(exc).__name__, flush=True)
                    return
                finally:
                    previous = time.monotonic()
                (run / "targeted-anchor-results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
                print("재생성 진단", case["id"], candidate["plan"]["action"], flush=True)


if __name__ == "__main__":
    main()
