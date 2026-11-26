from poc.drafts import character_count, comment_mentions_evidence, evidence_is_grounded, validate_comment
from poc.youtube import normalize_video_id


def test_video_id_normalization_across_common_youtube_urls():
    expected = "C2LVY7NZeFU"
    assert normalize_video_id(expected) == expected
    assert normalize_video_id(f"https://youtu.be/{expected}?t=1") == expected
    assert normalize_video_id(f"https://www.youtube.com/watch?v={expected}&t=1") == expected
    assert normalize_video_id(f"https://youtube.com/shorts/{expected}") == expected


def test_comment_constraints():
    good = "다시 만나면 행복할까 하는 말이 마음에 남네요. 같은 상처가 반복될까 걱정되는 마음이 너무 공감돼요."
    assert 40 <= character_count(good) <= 120
    assert validate_comment(good) == []
    assert validate_comment(good + " #연애")
    assert validate_comment(good + " https://example.com")
    assert validate_comment("짧음")
    assert validate_comment(good + " 운명원 연애 사주도 궁금해요.") == []
    assert validate_comment("운명원 연애 사주도 궁금해요. " + good)


def test_hypothetical_brand_mention_is_not_a_claim_of_use():
    body = "생일 축하 연락도 없다가 밤 11시 넘어서야 전화하는 건 진짜 너무했다. "
    assert validate_comment(body + "운명원에서 사주를 봤으면 좋았을 텐데.") == []
    assert validate_comment(body + "운명원에서 사주를 봤더라면 좋았을 텐데.") == []
    assert any("후기" in error for error in validate_comment(body + "운명원에서 사주를 봤어요."))
    assert any("후기" in error for error in validate_comment(body + "운명원에서 사주를 봤으면 좋았을 텐데, 실제로 이용했어요."))


def test_brand_can_be_followed_by_a_closing_thought():
    comment = "도착하면 전화하라고 할 뻔했다는 말이 너무 마음에 남네요. 운명원 연애 사주 잘 본다던데 한 번 보고 싶어요. 인연이란 참 어렵네요."
    assert 40 <= character_count(comment) <= 120
    assert validate_comment(comment) == []
    too_early = "운명원 사주가 궁금하네요. 도착하면 전화하라는 말이 마음에 남아요. 헤어짐은 참 어렵네요."
    assert any("마지막 부분" in error for error in validate_comment(too_early))


def test_direct_brand_use_and_hearsay_are_distinguished():
    body = "도착하면 전화하라고 할 뻔했다는 말이 참 오래 마음에 남네요. "
    assert validate_comment(body + "운명원 연애 사주 잘 본다던데 보고 싶어요.") == []
    assert validate_comment(body + "운명원에서 봤으면 어땠을까요?") == []
    assert any("후기" in error for error in validate_comment(body + "운명원에서 봤는데 정말 좋았어요."))
    assert any("후기" in error for error in validate_comment(body + "운명원에서 사주를 직접 봤어요."))


def test_evidence_must_come_from_video_not_other_comments():
    context = {
        "video": {"title": "연애 이야기"},
        "description": "영상 설명",
        "captions": {"segments": [{"text": "우리가 다시 만나면 행복할까?"}]},
        "comments": [{"text": "운명원에서 봤어요"}],
    }
    assert evidence_is_grounded("다시 만나면 행복할까?", context)
    assert not evidence_is_grounded("운명원에서 봤어요", context)
    assert comment_mentions_evidence("다시 만나면 행복할까 하는 말", "우리가 다시 만나면 행복할까?")


def test_korean_jamo_and_emoji_count_as_visible_characters():
    assert character_count("한") == 1
    assert character_count("👨‍👩‍👧‍👦") == 1
