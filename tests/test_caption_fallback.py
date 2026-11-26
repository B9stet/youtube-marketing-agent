import poc.youtube as youtube


def test_absent_captions_are_distinct_from_download_error(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "CACHE_DIR", tmp_path)
    absent = youtube._get_captions("C2LVY7NZeFU", {"subtitles": {}, "automatic_captions": {}})
    assert absent["state"] == "absent"
    assert absent["segments"] == []

    class FailingDownloader:
        def __init__(self, options):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def download(self, urls):
            raise RuntimeError("요청 제한")

    monkeypatch.setattr(youtube, "YoutubeDL", FailingDownloader)
    failed = youtube._get_captions(
        "C2LVY7NZeFU",
        {"subtitles": {"ko": [{"url": "https://example.invalid"}]}, "automatic_captions": {}},
    )
    assert failed["state"] == "error"
    assert failed["error"] == "요청 제한"


def test_description_can_be_used_without_caption_segments():
    from poc.drafts import evidence_is_grounded

    context = {"video": {"title": "연애 이야기"}, "description": "다시 만나면 행복할까?", "captions": {"segments": []}}
    assert evidence_is_grounded("다시 만나면 행복할까?", context)


def test_no_caption_still_sends_description_and_comments_to_ai(monkeypatch):
    import json
    import poc.drafts as drafts

    seen = {}

    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            result = {
                "concern": "재회가 행복할지 고민한다.",
                "evidence_quote": "다시 만나면 행복할까?",
                "comment": "다시 만나면 행복할까 하는 말이 마음에 남네요. 같은 상처가 반복될까 걱정되는 마음이 너무 공감돼요.",
            }
            return {"output": [{"content": [{"type": "output_text", "text": json.dumps(result, ensure_ascii=False)}]}]}

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, json):
            seen.update(json)
            return Response()

    monkeypatch.setattr(drafts.httpx, "Client", Client)
    context = {
        "video": {"title": "연애 이야기"},
        "description": "다시 만나면 행복할까?",
        "captions": {"state": "absent", "segments": []},
        "comments": [{"text": "공감해요", "likes": 3}],
    }
    result = drafts.generate_draft(context)
    material = json.loads(seen["input"][1]["content"])
    assert material["description"] == context["description"]
    assert material["popular_comments"] == context["comments"]
    assert material["captions"] == []
    assert result["grounded"]
