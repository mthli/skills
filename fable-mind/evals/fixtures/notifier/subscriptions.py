"""Subscription registry for the notifier service."""

_REGISTRY = {}


def add_subscriber(topic, email, tags=[]):
    """Register *email* for *topic*.

    Each subscriber carries a list of tags used to group entries in the
    rendered digest; the subscription's own topic is always included as
    a tag.  Returns the subscriber list for the topic.
    """
    tags.append(topic)
    _REGISTRY.setdefault(topic, []).append({"email": email, "tags": tags})
    return _REGISTRY[topic]


def subscribers(topic):
    """Return the list of subscribers registered for *topic*."""
    return _REGISTRY.get(topic, [])


def reset():
    """Clear the registry (used between tests)."""
    _REGISTRY.clear()
