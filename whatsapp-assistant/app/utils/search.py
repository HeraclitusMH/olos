"""Lightweight query/tag normalization for memory search.

The planner produces tags at memory_store time. ``build_search_query`` and
``generate_tags`` are used at retrieve / update time to (a) clean a
natural-language query before handing it to ``plainto_tsquery`` and
(b) generate a small tag set without an extra LLM round-trip.
"""

from __future__ import annotations

import re

# Common stop words that add no signal to a tsquery and bloat tag sets.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "he",
        "her",
        "him",
        "his",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "she",
        "so",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "whats",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "know",
        "remind",
        "remember",
        "about",
        "if",
        "can",
        "could",
        "should",
        "again",
    }
)


def build_search_query(user_query: str) -> str:
    """Strip question/filler words and return space-joined key terms.

    Example::

        build_search_query("What's my Indonesian phone number?")
        # -> "indonesian phone number"
    """
    if not isinstance(user_query, str):
        return ""
    lowered = user_query.lower()
    # Replace anything that isn't a letter/digit/underscore/+/- with whitespace.
    cleaned = re.sub(r"[^\w+\-]+", " ", lowered)
    tokens = [
        t
        for t in cleaned.split()
        if t and len(t) > 1 and t not in _STOPWORDS
    ]
    return " ".join(tokens)


async def generate_tags(content: str, max_tags: int = 5) -> list[str]:
    """Extract up to ``max_tags`` simple tag tokens from ``content``.

    Used by ``memory_update`` to keep tags fresh without paying for another
    LLM call. Initial ``memory_store`` tags come from the planner.
    """
    if not isinstance(content, str):
        return []
    lowered = content.lower()
    cleaned = re.sub(r"[^\w]+", " ", lowered)
    tags: list[str] = []
    for token in cleaned.split():
        if len(token) < 3 or token in _STOPWORDS:
            continue
        if token in tags:
            continue
        tags.append(token)
        if len(tags) >= max_tags:
            break
    return tags
