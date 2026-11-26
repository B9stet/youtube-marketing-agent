import pytest

from poc.drafts import character_count, validate_comment
from poc.natural_variants import apply_variant


def test_opt_in_rate_and_one_edit_are_reproducible():
    text = "그때 두 사람 모두 많이 힘들었을 것 같아요. 서로의 마음이 달랐다는 점이 참 안타깝네요."
    assert apply_variant(text, rate=0, case_id="one")["comment"] == text
    first = apply_variant(text, rate=1, case_id="one")
    assert first == apply_variant(text, rate=1, case_id="one")
    assert first["applied"]
    assert first["comment"].replace(first["after"], first["before"], 1) == text
    assert not validate_comment(first["comment"])


def test_spacing_and_spelling_can_be_controlled_separately():
    text = "서로 말할 수 있을지 고민하는 장면이 인상적이에요. 두 사람 모두 행복했으면 좋겠어요."
    spacing = apply_variant(text, rate=1, kinds=("spacing",), case_id="s")
    spelling = apply_variant(text, rate=1, kinds=("spelling",), case_id="t")
    assert spacing["before"] == "할 수"
    assert "말할수" in spacing["comment"]
    assert spelling["before"] == "좋겠"
    assert "좋겟어요" in spelling["comment"]


def test_quoted_video_evidence_is_not_modified():
    text = "'다시 만나면 행복할 것 같아요'라는 말을 듣고 보니 두 사람의 그 복잡한 마음이 이해돼요."
    result = apply_variant(text, evidence_quote="다시 만나면 행복할 것 같아요", rate=1, kinds=("spacing",))
    assert result["comment"] == text
    assert result["reason"] == "no_safe_candidate"


def test_unquoted_evidence_and_brand_sentence_are_protected():
    text = "서로 말할 수 있을까 하는 말이 마음에 남네요. 운명원에서 볼 수 있는 내용도 궁금하네요."
    result = apply_variant(text, evidence_quote="서로 말할 수 있을까", rate=1, kinds=("spacing",))
    assert result["comment"] == text
    assert result["reason"] == "no_safe_candidate"


def test_variant_that_would_break_minimum_length_is_skipped():
    text = "이 장면은 정말 힘들었을 것 같아요." + "가" * 30
    text = text[:40]
    assert character_count(text) == 40
    assert "것 같" in text
    result = apply_variant(text, rate=1, kinds=("spacing",))
    assert result["comment"] == text
    assert result["reason"] == "no_safe_candidate"


@pytest.mark.parametrize("rate", [-1, 1.01, float("nan"), True, "0.3"])
def test_invalid_rates_fail_closed(rate):
    with pytest.raises(ValueError):
        apply_variant("댓글입니다.", rate=rate)
