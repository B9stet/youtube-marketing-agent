from poc.ending_diversity import analyze_endings


def test_repeated_how_questions_are_flagged_without_banning_questions():
    endings = ["운명원 사주에서는 어떻게 나올까요?", "운명원 사주로는 어떻게 볼까요?", "운명원 사주도 궁금하네."]
    result = analyze_endings(endings)
    assert result["repeated_templates"] == {"어떻게_볼지_나올지": 2}
    assert not result["question_dominant"]
    questions = analyze_endings(["운명원 사주로 알 수 있을까요?", "운명원 사주에서는 어떨까요?", "운명원 사주로도 그럴까?"])
    assert questions["question_dominant"]
    assert questions["repeated_templates"] == {}
    assert analyze_endings(["운명원 사주 궁합에 나오는 편인가."])["question_count"] == 1
    leads = analyze_endings(["운명원 사주라도 보고 고민해요.", "운명원 사주라도 보면서 기다려요."])
    assert leads["repeated_leads"] == {"운명원 사주라도": 2}
