"""Probe whether the new interest signal distinguishes two concrete endings."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.contextual_brand_poc import diagnose
from scripts.run_jev_humanizer import prepare_existing_jev
from scripts.run_contextual_brand_poc import RECENT


def main() -> None:
    run = sorted((ROOT / ".agent/contextual-bridge-poc").glob("run-*"))[-1]
    records = json.loads((run / "rerouted-results.json").read_text(encoding="utf-8"))
    chosen = [("birthday_criticized", "관심 연결이 약한 후보"), ("chemistry", "궁합에 대한 관심을 표현한 후보")]
    output = []
    prepare_existing_jev()
    with httpx.Client(timeout=120) as client:
        for i, (key, label) in enumerate(chosen):
            if i:
                time.sleep(8)
            record = next(r for r in records if r["case"]["id"] == key)
            case, comment = record["case"], record["outcome"]["comment"]
            ending = comment[len(case["body"]):].strip()
            try:
                result = diagnose(case, comment, ending, RECENT, client)
                output.append({"id": key, "label": label, "comment": comment, "diagnosis": result})
                print(key, "interest_gap", result["scores"]["interest_gap"], flush=True)
            except Exception as exc:
                output.append({"id": key, "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
                print(key, "error", flush=True)
                break
            (run / "interest-probe.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
