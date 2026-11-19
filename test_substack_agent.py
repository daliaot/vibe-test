"""Offline tests: no API key or internet needed.  Run:  python -m pytest -q"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import substack_agent as sa

NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(days=7)
BODY = "<p>" + "In 1858 the Great Stink forced London to build modern sewers. " * 30 + "</p>"


def post_data(**over):
    data = {"audience": "everyone", "type": "newsletter", "post_date": NOW.isoformat(),
            "body_html": BODY, "title": "The Great Stink", "subtitle": "Sewers",
            "canonical_url": "https://hist.substack.com/p/stink",
            "publishedBylines": [{"name": "Ann", "publicationUsers": [
                {"publication": {"name": "History Bits"}}]}]}
    data.update(over)
    return data


def test_url_keys():
    assert sa.canonical_post_key("https://Hist.substack.com/p/the-stink?utm=x") == "hist/the-stink"
    assert sa.canonical_post_key("https://substack.com/@ann/note/c-123") is None
    assert sa.canonical_post_key("https://hist.substack.com/") is None
    assert sa.canonical_post_key("https://example.com/p/thing") is None


def test_filters():
    assert sa.judge_post(post_data(), SINCE) is None
    assert sa.judge_post(post_data(audience="only_paid"), SINCE) == "paid or subscriber-only"
    assert sa.judge_post(post_data(type="thread"), SINCE) == "thread"
    assert sa.judge_post(post_data(type="podcast", body_html="<p>Listen now</p>"), SINCE) == "podcast only"
    assert sa.judge_post(post_data(type="podcast"), SINCE) is None  # podcast with a full written post
    old = (NOW - timedelta(days=9)).isoformat()
    assert sa.judge_post(post_data(post_date=old), SINCE) == "older than the time window"
    assert sa.judge_post(post_data(body_html="<p>short</p>"), SINCE) == "too short or text not public"


def make_item(quote):
    post = sa.Post("u", "hist", "t", "", "Ann", "History Bits", NOW, sa.html_to_text(BODY))
    return sa.Item(post=post, headline="h", why=["a", "b"],
                   claims=[{"claim": "c", "quote": quote}])


def test_quote_check():
    assert sa.quotes_found(make_item("the Great Stink forced London")) is None
    assert sa.quotes_found(make_item("THE GREAT STINK — forced London!")) is None  # punctuation/case
    assert "not found" in sa.quotes_found(make_item("Paris built sewers in 1858"))
    assert "not found" in sa.quotes_found(make_item("sewers"))  # too short to count


class FakeClaude:
    """Picks every post; writes a good item except for post 'bad', whose quote is invented."""

    def __init__(self, model):
        pass

    def search_urls(self, prompt, max_searches=8):
        return ["https://hist.substack.com/p/good", "https://hist.substack.com/p/bad",
                "https://biz.substack.com/p/paid", "https://substack.com/@x/note/c-1"]

    def json(self, prompt, schema, effort="medium"):
        if "ranked_ids" in schema["properties"]:
            return {"ranked_ids": [0, 1]}
        if "headline" in schema["properties"]:
            quote = "invented quote about Paris" if "Title: bad" in prompt else "Great Stink forced London"
            return {"headline": "London's smell built its sewers", "why_line_1": "One.",
                    "why_line_2": "Two.", "claims": [{"claim": "x", "quote": quote}]}
        return {"every_claim_supported": True, "problems": ""}


def fake_get(url, **kw):
    slug = url.rsplit("/", 1)[1]
    data = post_data(title=slug, canonical_url=url.replace("/api/v1/posts/", "/p/"),
                     audience="only_paid" if slug == "paid" else "everyone")
    resp = mock.Mock()
    resp.json.return_value = data
    return resp


def test_end_to_end(tmp_path):
    cfg = sa.load_config(sa.HERE / "config.toml")
    cfg["output_folder"] = str(tmp_path)
    with mock.patch.object(sa, "Claude", FakeClaude), mock.patch.object(sa.requests, "get", fake_get):
        report = sa.run(cfg)
    assert "**1. London's smell built its sewers**" in report
    assert "**2." not in report
    assert "bad" in report and "quote not found" in report
    assert report.strip().splitlines()[-1] == "Read 3 sources; dropped 1 item I could not check."
    assert list(tmp_path.glob("*.md"))
