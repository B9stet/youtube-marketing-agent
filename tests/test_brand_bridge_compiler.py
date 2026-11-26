import pytest

from poc.brand_bridge_compiler import check_result, compile_issues, select_from_signals


def test_compiler_is_order_independent_and_rejects_unknown_rules():
    first = compile_issues(["stance", "abrupt"])
    assert first == compile_issues(["abrupt", "stance"])
    assert "잘못한 행동을 운이나" in first
    with pytest.raises(ValueError):
        compile_issues(["arbitrary provider text"])


def test_threshold_is_explicit_and_invalid_signals_are_rejected():
    assert select_from_signals({"jump": .34, "claim": .35, "stance": .8}) == ["unsupported", "stance"]
    assert select_from_signals({"advertorial": .48}) == ["advertorial"]
    for bad in (True, float("nan"), -0.1, 1.1):
        with pytest.raises(ValueError):
            select_from_signals({"jump": bad})


def test_observable_brand_constraints():
    body = "다시 만나면 행복할까 고민하는 장면이 현실적이네요. 같은 이유로 또 헤어질까 봐 걱정돼요."
    assert check_result(body, "운명원 사주에선 이런 인연을 어떻게 볼지 궁금하네요.") == []
    assert check_result(body + " 운명원", "사주가 궁금해요.")
    assert check_result(body, "운명원 사주가 궁금해요. 링크는 여기예요.")
    assert check_result(body, "운명원 사주 https://example.com")
