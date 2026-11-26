"""Record transparent editorial alternatives after inspecting model candidates."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poc.brand_bridge_compiler import check_result
from poc.ending_diversity import ending_style

ENDINGS = {
    "Cr2XlCDrxUM": ("질문", "다름을 인정해도 결국 같은 이유로 또 부딪히게 될지 운명원 연애 사주에선 어떻게 나올까요?",
                    "retained_previous", "기존 질문이 장면의 갈등을 구체적으로 이어가고 있어 유지한다. 새 후보는 행동을 타고난 기질 탓으로 돌렸다."),
    "6Hq96MZqego": ("망설임", "연락을 끊어야 할지 운명원 연애 사주까지 보면서 오래 고민할 것 같아요.",
                    "assistant_manual", "이별 후 연락을 계속할지 망설이는 입장을 결론 없이 남긴다."),
    "8xAfwDQtDj8": ("소신", "운명원 사주에서 재회운이 좋게 나와도 상대 마음까진 바꿀 수 없을 것 같아요.",
                    "assistant_manual", "사주 결과를 가정하더라도 재회를 거절한 상대의 선택을 우선한다."),
    "n7ho4C1aKVw": ("가벼운 추측", "이 정도 티키타카면 운명원 사주로 봐도 궁합 좋게 나올 듯ㅋㅋ",
                    "assistant_manual", "칭찬을 주고받는 가벼운 장면에 맞춰 결과를 확정하지 않는 추측을 쓴다."),
    "U7PdMvogEGQ": ("현실적인 반응", "서로 일정부터 물어봐야지, 운명원 사주 궁합은 그다음이고 ㅋㅋ",
                    "assistant_manual", "확인되지 않은 출장 빈도를 사주로 넘겨짚지 않고 직접 묻는 편을 우선한다."),
}


def main() -> None:
    run = sorted((ROOT / ".agent/diverse-comment-poc").glob("run-*"))[-1]
    path = run / "candidates.json"
    source = run / "model-candidates.json"
    if not source.exists():
        source.write_bytes(path.read_bytes())
    records = json.loads(source.read_text(encoding="utf-8"))
    selections = {}
    for record in records:
        case = record["case"]
        function, ending, provenance, reason = ENDINGS[case["id"]]
        errors = check_result(case["body"], ending)
        if errors:
            raise ValueError(f"{case['id']}: {'; '.join(errors)}")
        record["candidates"].append({"function": function, "ending": ending, "connection": reason,
                                     "comment": f"{case['body']} {ending}", "errors": errors,
                                     "style": ending_style(ending), "provenance": provenance})
        selections[case["id"]] = {"candidate_index": len(record["candidates"]) - 1, "reason": reason, "provenance": provenance}
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (run / "selections.json").write_text(json.dumps(selections, ensure_ascii=False, indent=2), encoding="utf-8")
    print("이전 질문 1개 유지, 직접 작성한 대안 4개를 시안으로 선택했습니다.")


if __name__ == "__main__":
    main()
