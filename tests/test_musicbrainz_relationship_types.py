"""The typed MusicBrainz relationship queries must name every type the enricher publishes.

musicbrainz-graph-enricher publishes its relationship types as a contract
(``contracts/projections/v1/relationship-types.json``), and catalog-api promotes that file byte for byte
into ``contracts/musicbrainz-projections/v1`` (``scripts/check-contracts.py`` verifies its digest). Every
test here reads the promoted file, so CI fails the moment the published list holds a type the queries omit,
with no sibling repository in the workspace and nothing skipped. A new enricher type is picked up by
promoting the new contract and adding the type to ``MB_ARTIST_RELATIONSHIP_TYPES``.
"""

import json
from pathlib import Path

from api.queries.musicbrainz_queries import _MB_RELATIONSHIPS_CYPHER, MB_ARTIST_RELATIONSHIP_TYPES, MB_SOURCED_RELATIONSHIP_TYPES


PROMOTED = Path(__file__).resolve().parents[1] / "contracts" / "musicbrainz-projections" / "v1" / "relationship-types.json"


def _published_edge_types() -> set[str]:
    return set(json.loads(PROMOTED.read_text())["edge_types"])


def test_the_promoted_contract_is_the_enrichers_relationship_type_contract() -> None:
    published = json.loads(PROMOTED.read_text())
    assert published["contract"] == "groovemap.musicbrainz-graph-enricher.relationship-types"
    assert published["version"] == 1
    assert published["store"] == "neo4j"


def test_the_query_names_every_published_type() -> None:
    omitted = _published_edge_types() - set(MB_ARTIST_RELATIONSHIP_TYPES)
    assert not omitted, f"the published contract has types the typed query omits: {sorted(omitted)}; add them to MB_ARTIST_RELATIONSHIP_TYPES"


def test_the_query_names_no_type_the_enricher_does_not_publish() -> None:
    extra = set(MB_ARTIST_RELATIONSHIP_TYPES) - _published_edge_types()
    assert not extra, f"the typed query names types the enricher does not publish: {sorted(extra)}"


def test_the_sourced_route_carries_the_same_types_plus_the_medium_edge() -> None:
    assert set(MB_SOURCED_RELATIONSHIP_TYPES) == _published_edge_types() | {"ISSUED_ON"}


def test_query_text_carries_exactly_the_listed_types_in_both_directions() -> None:
    assert _MB_RELATIONSHIPS_CYPHER.count(f"[r:{'|'.join(MB_ARTIST_RELATIONSHIP_TYPES)}]") == 2
    assert "[r]->" not in _MB_RELATIONSHIPS_CYPHER
