"""Demonstrates the tag-leak bug.

Expected output:
    == news ==
      alice@example.com [news]
    == sports ==
      bob@example.com [sports]

Actual output: both subscribers end up tagged [news, sports] -- bob
inherits alice's tag and alice retroactively gains bob's.
"""
from subscriptions import add_subscriber, reset
from digest import render_digest

reset()
add_subscriber("news", "alice@example.com")
add_subscriber("sports", "bob@example.com")
print(render_digest(["news", "sports"]))
