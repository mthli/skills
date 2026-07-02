from subscriptions import add_subscriber, reset
from digest import render_digest


def test_render_lists_subscriber_with_tags():
    reset()
    add_subscriber("news", "carol@example.com", ["vip"])
    out = render_digest(["news"])
    assert "== news ==" in out
    assert "carol@example.com [vip, news]" in out


def test_render_empty_topic_shows_header():
    reset()
    out = render_digest(["sports"])
    assert out == "== sports =="
