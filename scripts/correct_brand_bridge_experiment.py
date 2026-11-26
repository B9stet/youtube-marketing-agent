"""Compile fixed correction rules from Jev signals and inspect the edits."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.brand_bridge_compiler import check_result, compile_issues, select_from_signals
from poc.config import AI_BASE_URL, AI_MODEL
from poc.drafts import _response_text, character_count

def jev_signals(rows: list[dict], key: str) -> dict:
    matched = [r for r in rows if r["id"] == key and "answers" in r]
    if not matched:
        raise ValueError(f"{key}의 Jev 결과가 없습니다.")
    result = {}
    for signal in ("jump", "claim", "stance", "advertorial"):
        values = []
        for row in matched:
            suffix = "A" if row["order"] == "generated_A" else "B"
            item = row["answers"].get(f"{signal}_{suffix}")
            if item is not None:
                values.append(float(item["noul"]))
        if values:
            result[signal] = max(values)
    return result


def correct(record: dict, signals: dict) -> dict:
    case = record["case"]
    original = record["candidates"][0]
    selected = select_from_signals(signals)
    result = {"id": case["id"], "original": original, "signals": signals, "selected": selected}
    if signals["stance"] >= .75 or signals["claim"] >= .75:
        result.update({"decision": "hold", "reason": "입장 변경 또는 근거 없는 단정 신호가 높아 자동 교정 대신 검토합니다."})
        return result
    if not selected:
        result.update({"decision": "keep", "reason": "탐색 기준 이상인 Jev 신호가 없습니다."})
        return result
    prompt = compile_issues(selected)
    material = {"scene": case["scene"], "body": case["body"], "ending": original["ending"],
                "emotion": case["emotion"], "diagnosis": signals}
    payload = {"model": AI_MODEL, "input": [{"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(material, ensure_ascii=False)}], "max_output_tokens": 2500, "store": False}
    started = time.perf_counter()
    with httpx.Client(timeout=120) as client:
        for attempt in range(2):
            try:
                r = client.post(f"{AI_BASE_URL}/responses", json=payload)
                r.raise_for_status()
                data = r.json()
                parsed = json.loads(_response_text(data))
                decision = parsed["decision"]
                if decision not in ("rewrite", "keep", "hold"):
                    raise ValueError("invalid decision")
                ending = str(parsed.get("ending", "")).strip()
                if decision == "keep":
                    ending = original["ending"]
                errors = check_result(case["body"], ending) if decision != "hold" else []
                result.update({"decision": decision, "reason": str(parsed.get("reason", "")), "ending": ending,
                               "comment": f"{case['body']} {ending}" if ending else "",
                               "characters": character_count(f"{case['body']} {ending}") if ending else None,
                               "errors": errors, "usage": data.get("usage", {}), "model": data.get("model", AI_MODEL),
                               "seconds": round(time.perf_counter() - started, 2), "compiled_prompt": prompt})
                return result
            except Exception as exc:
                result.setdefault("failures", []).append(f"{type(exc).__name__}: {str(exc)[:150]}")
                if attempt == 0:
                    payload["input"].append({"role": "user", "content": "요구한 JSON 필드와 decision을 지켜 다시 출력하세요."})
    result["decision"] = "error"
    return result


def main() -> None:
    eligible = [p for p in sorted((ROOT / ".agent/brand-bridge-research").glob("run-*"))
                if (p / "results.json").exists() and (p / "jev-pairwise.json").exists()]
    if not eligible:
        raise RuntimeError("Jev 비교가 완료된 실행 결과를 찾지 못했습니다.")
    run = eligible[-1]
    records = json.loads((run / "results.json").read_text(encoding="utf-8"))
    rows = json.loads((run / "jev-pairwise.json").read_text(encoding="utf-8"))
    output = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = {}
        for key, record in records.items():
            if not record.get("candidates"):
                output[key] = {"id": key, "decision": "hold", "reason": "생성 단계에서 보류됐습니다."}
                continue
            try:
                tasks[pool.submit(correct, record, jev_signals(rows, key))] = key
            except ValueError as exc:
                output[key] = {"id": key, "decision": "hold", "reason": str(exc)}
        for future in as_completed(tasks):
            key = tasks[future]
            try:
                output[key] = future.result()
            except Exception as exc:
                output[key] = {"id": key, "decision": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            (run / "corrections.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{key}: {output[key]['decision']}", flush=True)
    (run / "corrections.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
