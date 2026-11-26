import pytest

from scripts.run_new_video_comment_poc import ending_after_review, window


def test_caption_window_preserves_order_and_requires_actual_evidence():
    segments = [
        {"start": "00:00:03.000", "text": "나중에 사과했어"},
        {"start": "00:00:01.000", "text": "먼저 얘기했어"},
        {"start": "00:00:01.000", "text": "상대가 답했어"},
        {"start": "00:00:05.000", "text": "구간 밖"},
    ]
    chosen = window(segments, 1, 3, "먼저 얘기했어")
    assert [s["text"] for s in chosen] == ["먼저 얘기했어", "상대가 답했어", "나중에 사과했어"]
    with pytest.raises(ValueError):
        window(segments, 1, 3, "구간 밖")


def test_batch_comparison_uses_the_edited_ending_when_there_is_one():
    record = {"initial": {"ending": "기존 질문?"}, "correction": {"answer": {"decision": "rewrite"}, "ending": "새 문장."}}
    assert ending_after_review(record) == "새 문장."
