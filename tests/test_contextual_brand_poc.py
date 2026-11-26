import hashlib
import json

import httpx
import pytest

from poc.contextual_brand_poc import JEV_QUESTIONS, confirmed_source_errors, correct, diagnose, generate, plan_correction, post_review_status
from poc.brand_bridge_compiler import compile_issues


def scores(**values):
    result = {key: .1 for key in JEV_QUESTIONS}
    result.update(values)
    return result


def test_new_emotion_and_resolution_signals_compile_fixed_rules():
    plan = plan_correction(scores(emotion_flattened=.6, easy_resolution=.8))
    assert plan["action"] == "rewrite"
    assert plan["selected"] == ["emotion_flattened", "easy_resolution"]
    compiled = compile_issues(plan["selected"], base_prompt="BASE")
    assert compiled.startswith("BASE\n")
    assert "가벼운 후회로 축소하지 않는다" in compiled
    assert "불확실성과 갈등이 남아 있어도 된다" in compiled
    assert plan_correction(scores(interest_gap=.5))["selected"] == ["interest_gap"]


def test_routes_unresolved_roles_and_bad_body_before_rewrite():
    assert plan_correction(scores(role_error=.36, emotion_flattened=.7))["action"] == "review_facts"
    assert plan_correction(scores(timeline_error=.36))["action"] == "review_facts"
    assert plan_correction(scores(timeline_error=.26), confirmed_source_errors=["사과 순서 오류"])["action"] == "review_facts"
    assert plan_correction(scores(body_flat=.4, emotion_flattened=.6))["action"] == "revise_body"
    assert plan_correction(scores(), ["길이 위반"])["action"] == "review_constraints"
    assert plan_correction(scores())["action"] == "keep"
    assert plan_correction(scores(body_stance=.9), body_changed=False)["action"] == "keep"
    assert plan_correction(scores(body_stance=.9), body_changed=True)["action"] == "review_facts"
    assert plan_correction(scores(stance=.82), body_changed=False)["action"] == "rewrite"
    assert post_review_status({"action": "review_facts"}, body_changed=True) == "hold"
    assert post_review_status({"action": "keep"}, body_changed=True) == "review_body_rewrite"
    assert post_review_status({"action": "rewrite"}, scores={"stance": .8}) == "hold"
    with pytest.raises(ValueError):
        plan_correction({"emotion_flattened": .5})


def test_jev_batches_separate_questions_on_the_authorized_free_route(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "example-test-key")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://api.experientiallabs.ai")
    captured = []

    def reply(request):
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(200, json={"model": "test", "answers": {name: {"noul": .2} for name in JEV_QUESTIONS}})

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        result = diagnose({"scene": "생일에 늦은 연락", "body": "기다린 마음이 서러워요."}, "예시 댓글", "끝 문장", [], client)
    assert set(result["scores"]) == set(JEV_QUESTIONS)
    assert captured[0]["model"] == "jev-latest:free"
    assert captured[0]["questions"]["emotion_flattened"]["type"] == "noul"
    assert captured[0]["questions"]["easy_resolution"]["type"] == "noul"


def test_source_review_is_scoped_to_the_exact_comment():
    comment = "먼저 다른 사람에게 사과했다."
    findings = [{"case_id": "scene1", "comment_sha256": hashlib.sha256(comment.encode()).hexdigest(), "errors": ["순서가 다름"]}]
    assert confirmed_source_errors(findings, "scene1", comment) == ["순서가 다름"]
    assert confirmed_source_errors(findings, "scene1", "앞서 당사자에게도 사과했다.") == []
    assert confirmed_source_errors(findings, "scene2", comment) == []


def test_generation_passes_the_requested_question_topic_without_changing_the_body():
    body = "다시 만나면 행복할까 고민하는 장면이 현실적이네요. 같은 상처가 반복될까 걱정돼요."
    preference = {"form": "열린 질문", "topic": "같은 이유로 다시 부딪힐지"}
    captured = []

    def reply(request):
        payload = json.loads(request.content)
        captured.append(payload)
        answer = {"action": "draft", "scene_fact": "재회 고민", "emotional_core": "반복될까 걱정", "bridge_reason": "질문", "ending": "운명원 사주라면 같은 문제로 다시 부딪힐지 알 수 있을까요?"}
        return httpx.Response(200, json={"model": "test", "output_text": json.dumps(answer, ensure_ascii=False)})

    case = {"body": body, "scene": "재회를 고민한다", "connection_preference": preference}
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        result = generate(case, client)
    material = json.loads(captured[0]["input"][1]["content"])
    assert material["connection_preference"] == preference
    assert result["comment"].startswith(body + " ")
    assert result["errors"] == []


def test_correction_receives_the_other_endings_for_comparison():
    body = "다른 걸 틀렸다고 생각했던 지난 오해를 인정하니 아쉬운 마음이 느껴져요."
    ending = "같은 이유로 다시 부딪힐지 운명원 연애 사주에선 어떻게 나올까요?"
    recent = ["이 관계도 운명원 사주에서는 어떻게 볼까요?", "다른 인연은 어떻게 나올까요?"]
    captured = []

    def reply(request):
        payload = json.loads(request.content)
        captured.append(payload)
        answer = {"decision": "keep", "reason": "문맥에 맞음", "ending": ending}
        return httpx.Response(200, json={"model": "test", "output_text": json.dumps(answer, ensure_ascii=False)})

    case = {"body": body, "scene": "이전의 오해를 돌아본다", "recent_endings": recent}
    plan = {"action": "rewrite", "selected": ["repetition"]}
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        correct(case, {"comment": body + " " + ending, "ending": ending}, plan, client)
    material = json.loads(captured[0]["input"][1]["content"])
    assert material["recent_endings"] == recent
