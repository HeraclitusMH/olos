from __future__ import annotations

from app.utils.search import build_search_query, generate_tags


def test_build_search_query_strips_question_words() -> None:
    assert (
        build_search_query("What's my Indonesian phone number?")
        == "indonesian phone number"
    )


def test_build_search_query_handles_do_you_know() -> None:
    cleaned = build_search_query("Do you know what loop station I want?")
    assert "loop" in cleaned.split()
    assert "station" in cleaned.split()
    assert "what" not in cleaned.split()
    assert "do" not in cleaned.split()


def test_build_search_query_returns_empty_for_only_stopwords() -> None:
    assert build_search_query("what is it") == ""


def test_build_search_query_returns_empty_for_non_string() -> None:
    assert build_search_query(None) == ""  # type: ignore[arg-type]


def test_build_search_query_keeps_plus_and_minus() -> None:
    cleaned = build_search_query("My number is +62 812 3456")
    assert "+62" in cleaned.split()


async def test_generate_tags_extracts_unique_tokens() -> None:
    tags = await generate_tags("Boss RC-505 loop station")

    assert "boss" in tags
    assert "loop" in tags
    assert "station" in tags
    assert len(tags) <= 5


async def test_generate_tags_caps_at_max() -> None:
    content = "alpha bravo charlie delta echo foxtrot golf hotel india"
    tags = await generate_tags(content, max_tags=3)
    assert len(tags) == 3


async def test_generate_tags_drops_stopwords_and_short_tokens() -> None:
    tags = await generate_tags("I am the boss of it")
    assert "boss" in tags
    assert "i" not in tags
    assert "am" not in tags
