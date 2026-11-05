"""Autocomplete keeps the true best matches when the limit goes to Lucene, on a real Neo4j (audit H15, ``gm-catalog-api-4wgh``).

Wildcard terms are constant-score, so the old query scored every match 1.0 and ``ORDER BY score LIMIT n`` kept an
arbitrary n; passing the limit to Lucene made it faster but kept a different arbitrary set. The boosts must (1) match
exactly the names the old query matched, (2) put a name equal to what was typed first, (3) make a limited call return
the head of the unlimited ranking, and (4) behave the same for artist, label, genre, style and person.

The fulltext indexes are created with the production analyzer (``standard-no-stop-words``). Nothing here touches a
shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries import credits_queries as credits
from api.queries import neo4j_queries as q
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_NAMES = [
    "David Bowie",
    "David Bowie,",
    "David Bowie Tribute",
    "David Bowie & The Buzz",
    "David Bowie With The Lower Third",
    "Bowie",
    "David Bowman",
    "Davidson Bowie",
    "The Beatles",
    "The Beatles (2)",
    "Not The Beatles",
    "The Beatles Revival",
    "Indecent Noise",
    "Indecent Noise Machine",
    "The",
    "The The",
    "The The The",
    "Theodore",
    "John Lennon",
    "Joh",
    "Johnny Cash",
    "Joh Joh Music",
    "Jay-Z",
    "Jay-Z (2)",
    "Jay Zimmerman",
    "Big Jay",
    "Z",
    "Zebra",
    "Wiz Z",
    "AC/DC",
    "AC/DC Tribute",
    "DC",
    "ACDC",
    "Dcs Band",
    "D'Angelo",
    "Nino D'Angelo",
    "Angelo",
    "Guns N' Roses",
    "Guns Of Navarone",
    "Roses",
]
_INDEXES = [
    ("artist_name_fulltext", "Artist"),
    ("label_name_fulltext", "Label"),
    ("genre_name_fulltext", "Genre"),
    ("style_name_fulltext", "Style"),
    ("person_name_fulltext", "Person"),
]


async def _seed(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    for index, label in _INDEXES:
        await run_query(
            driver,
            f"CREATE FULLTEXT INDEX {index} IF NOT EXISTS FOR (n:{label}) ON EACH [n.name] OPTIONS {{indexConfig: {{`fulltext.analyzer`: 'standard-no-stop-words'}}}}",
            database="neo4j",
            timeout=None,
        )
        await run_query(
            driver,
            f"UNWIND range(0, size($names) - 1) AS i CREATE (:{label} {{id: $prefix + toString(i), name: $names[i]}})",
            database="neo4j",
            timeout=None,
            names=_NAMES,
            prefix=label[0],
        )
    await run_query(driver, "CALL db.awaitIndexes(60)", database="neo4j", timeout=None)


_OLD = "CALL db.index.fulltext.queryNodes('artist_name_fulltext', $query) YIELD node RETURN node.name AS name"


async def _old_matches(driver: AsyncResilientNeo4jDriver, typed: str) -> set[str]:
    old_query = " AND ".join(q._escape_lucene_query(t) + "*" for t in typed.split())
    return {r["name"] for r in await run_query(driver, _OLD, database="neo4j", query=old_query)}


@pytest.mark.parametrize("typed", ["David Bowie", "the", "joh", "The Beatles", "indecent n", "bowie", "david bo"])
async def test_the_new_query_matches_a_subset_of_the_old_one_dropping_only_prefix_only_earlier_words(
    neo4j_driver: AsyncResilientNeo4jDriver, typed: str
) -> None:
    """Every word but the last must now be a whole word; the old query accepted a prefix there ('Davidson Bowie')."""
    await _seed(neo4j_driver)
    got = {r["name"] for r in await q.autocomplete_artist(neo4j_driver, typed, limit=1000)}
    old = await _old_matches(neo4j_driver, typed)
    assert got
    assert got <= old
    first = typed.split()[0].lower()
    for dropped in old - got:
        assert first not in dropped.lower().replace("&", " ").split(), dropped


@pytest.mark.parametrize(
    ("typed", "first"),
    [("David Bowie", "David Bowie"), ("The Beatles", "The Beatles"), ("Indecent Noise", "Indecent Noise"), ("joh", "Joh")],
)
async def test_a_name_equal_to_what_was_typed_comes_first(neo4j_driver: AsyncResilientNeo4jDriver, typed: str, first: str) -> None:
    await _seed(neo4j_driver)
    rows = await q.autocomplete_artist(neo4j_driver, typed, limit=10)
    assert rows[0]["name"] == first, [r["name"] for r in rows]


@pytest.mark.parametrize("typed", ["David Bowie", "the", "joh", "The Beatles"])
async def test_a_limited_call_keeps_the_highest_scoring_names(neo4j_driver: AsyncResilientNeo4jDriver, typed: str) -> None:
    """Whatever Lucene returns for limit n must score at least as high as every name it left out."""
    await _seed(neo4j_driver)
    everything = await q.autocomplete_artist(neo4j_driver, typed, limit=1000)
    for limit in (1, 3, 5):
        head = await q.autocomplete_artist(neo4j_driver, typed, limit=limit)
        assert len(head) == min(limit, len(everything))
        kept = {r["id"] for r in head}
        floor = min(r["score"] for r in head)
        assert all(r["score"] <= floor for r in everything if r["id"] not in kept), (typed, limit)
        assert [r["score"] for r in head] == sorted((r["score"] for r in head), reverse=True)


async def test_whole_word_matches_outrank_names_that_only_start_with_the_word(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    names = [r["name"] for r in await q.autocomplete_artist(neo4j_driver, "the", limit=1000)]
    assert names.index("The") < names.index("Theodore")
    assert names.index("The Beatles") < names.index("Theodore")


async def test_every_entity_type_ranks_the_same_way(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    artist = [r["name"] for r in await q.autocomplete_artist(neo4j_driver, "David Bowie", limit=10)]
    assert [r["name"] for r in await q.autocomplete_label(neo4j_driver, "David Bowie", limit=10)] == artist
    assert [r["name"] for r in await q.autocomplete_genre(neo4j_driver, "David Bowie", limit=10)] == artist
    assert [r["name"] for r in await q.autocomplete_style(neo4j_driver, "David Bowie", limit=10)] == artist
    assert [r["name"] for r in await credits.autocomplete_person(neo4j_driver, "David Bowie", limit=10)] == artist
    assert artist[0] == "David Bowie"


async def test_metacharacters_and_quotes_do_not_break_the_query(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    for typed in ['say "hi" now', "back\\slash x", "AC/DC live", "C++ band", "a:b c", "(unclosed paren"]:
        assert isinstance(await q.autocomplete_artist(neo4j_driver, typed, limit=5), list)


async def _names(driver: AsyncResilientNeo4jDriver, typed: str, limit: int = 1000) -> list[str]:
    return [r["name"] for r in await q.autocomplete_artist(driver, typed, limit=limit)]


async def test_hyphenated_input_requires_both_tokens(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    """'jay-z' is jay + z in the index. OR semantics would also return Zebra, Z, Big Jay and Wiz Z."""
    await _seed(neo4j_driver)
    names = await _names(neo4j_driver, "jay-z")
    assert names[0] == "Jay-Z"
    assert set(names) == {"Jay-Z", "Jay-Z (2)", "Jay Zimmerman"}  # both tokens, the last as a prefix: not Zebra, Z, Big Jay, Wiz Z


async def test_slashed_input_requires_both_tokens(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    names = await _names(neo4j_driver, "ac/dc")
    assert names[0] == "AC/DC"
    assert set(names) == {"AC/DC", "AC/DC Tribute", "Dcs Band"} - {"Dcs Band"}  # not DC, ACDC or Dcs Band


async def test_apostrophe_names_stay_one_token(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    names = await _names(neo4j_driver, "d'angelo")
    assert names[0] == "D'Angelo"
    assert "Angelo" not in names  # the analyzer keeps d'angelo whole, so a bare "angelo" does not match it
    guns = await _names(neo4j_driver, "guns n' roses")
    assert guns[0] == "Guns N' Roses"
    assert "Guns Of Navarone" not in guns and "Roses" not in guns


async def test_multiword_input_with_punctuation(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    names = await _names(neo4j_driver, "the jay-z")
    assert "Jay-Z" not in names  # a name without 'the' does not match
    assert await _names(neo4j_driver, "jay z") == await _names(neo4j_driver, "jay-z")  # punctuation and space are the same


async def test_input_without_word_characters_is_an_empty_result_not_an_error(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    assert await _names(neo4j_driver, "!!") == []
