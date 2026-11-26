import pytest

from scripts.run_unseen_video_poc import correction_plan, finalize_case, generation_errors, timestamp_seconds
from scripts.clarify_unseen_speakers import role_ambiguity_present, role_context_for


def scores(**kwargs):
    return {"formal": 0.1, "cliche_empathy": 0.1, "wordy": 0.1, "awkward": 0.1,
            "context_conflict": 0.1, **kwargs}


def test_manual_window_uses_precise_video_timestamps():
    assert timestamp_seconds("00:05:14.910") == pytest.approx(314.91)
    with pytest.raises(ValueError):
        timestamp_seconds("05:14")


def test_provisional_diagnoses_map_to_at_most_two_ordered_rules():
    result = correction_plan(scores(formal=0.4, cliche_empathy=0.5, wordy=0.8, awkward=0.6))
    assert result == {"action": "rewrite", "selected": ["wordy", "awkward"], "provisional": True}
    assert correction_plan(scores())["action"] == "keep"


def test_context_conflict_overrides_style_rewrite():
    result = correction_plan(scores(context_conflict=0.8, formal=0.99))
    assert result["action"] == "review_conflict"
    assert result["selected"] == []


def test_generation_requires_a_grounded_phrase_and_no_advertising():
    good = "생일 축하한다는 연락도 없었어요라는 말이 너무 속상하네요. 기다리는 사람 마음도 좀 알아줬으면 좋겠어요."
    assert generation_errors(good, "생일 축하한다는 연락도 없었어요") == []
    assert generation_errors(good, "전혀 다른 발언입니다")
    assert generation_errors(good + " 운명원도 궁금해요.", "생일 축하한다는 연락도 없었어요")


def test_failed_edit_is_not_mislabeled_as_a_successful_keep():
    original = "생일 축하한다는 연락도 없었어요라는 말이 너무 속상하네요. 기다리는 사람 마음도 좀 알아줬으면 좋겠어요."
    case = {"case_id": "x", "quote": "생일 축하한다는 연락도 없었어요"}
    record = {"generation": {"comment": original}, "diagnosis": {"scores": {}},
              "correction_plan": {"action": "rewrite"}, "rewrite": {"error": "invalid_response"}}
    finalize_case(case, record, 0)
    assert record["variant"]["comment"] == original
    assert "오류" in record["final_note"]


def test_role_guard_holds_the_observed_subject_confusion():
    assert role_ambiguity_present("상대방이 약속을 어겨서 서운하다고 할 때는 먼저 사과하세요.")
    assert role_ambiguity_present("상대방이 나랑 한 약속을 어겨서 서운하다고 할 때는")
    assert not role_ambiguity_present("약속을 어겨서 서운하다는 말에 먼저 사과하면 좋겠어요.")


def test_role_context_does_not_turn_a_question_into_advice():
    assert "조언의_대상" in role_context_for("XbPld5XoD7Y_practical_advice")
    assert "조언의_대상" not in role_context_for("XbPld5XoD7Y_question_reaction")
