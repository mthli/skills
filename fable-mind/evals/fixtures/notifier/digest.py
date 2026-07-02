"""Render a plain-text digest of all subscriptions."""

from subscriptions import subscribers


def render_digest(topics):
    lines = []
    for topic in topics:
        lines.append(f"== {topic} ==")
        for sub in subscribers(topic):
            tag_str = ", ".join(sub["tags"])
            lines.append(f"  {sub['email']} [{tag_str}]")
    return "\n".join(lines)
