"""The typed MusicBrainz relationship query must name every type the enricher writes.

The enricher lives in another repository, so this test pins its MB_RELATIONSHIP_MAP as a
snapshot (tests/fixtures/musicbrainz_relationship_map.json). The snapshot is checked against the
query's type list on every run. When the enricher's source is present in the workspace, the
snapshot is also checked against the live map, so an added enricher type fails here until the
query and the snapshot both name it.
"""

import ast
import json
import os
from pathlib import Path

from api.queries.musicbrainz_queries import _MB_RELATIONSHIPS_CYPHER, MB_ARTIST_RELATIONSHIP_TYPES


SNAPSHOT = Path(__file__).parent / "fixtures" / "musicbrainz_relationship_map.json"
ENRICHER_PROJECTIONS = (
    Path(os.environ.get("GIT_WORKSPACE", Path.home() / "workspaces"))
    / "github/groovemap-music/musicbrainz-graph-enricher/brainzgraphinator/_projections.py"
)


def _snapshot_types() -> set[str]:
    return set(json.loads(SNAPSHOT.read_text())["map"].values())


def _live_map(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "MB_RELATIONSHIP_MAP":
            return ast.literal_eval(node.value)  # type: ignore[no-any-return]
    raise AssertionError(f"MB_RELATIONSHIP_MAP not found in {path}")


def test_query_names_every_type_in_the_snapshot() -> None:
    assert set(MB_ARTIST_RELATIONSHIP_TYPES) == _snapshot_types()


def test_query_text_carries_exactly_the_listed_types_in_both_directions() -> None:
    assert _MB_RELATIONSHIPS_CYPHER.count(f"[r:{'|'.join(MB_ARTIST_RELATIONSHIP_TYPES)}]") == 2
    assert "[r]->" not in _MB_RELATIONSHIPS_CYPHER


def test_snapshot_matches_the_enricher_when_its_source_is_in_the_workspace() -> None:
    if not ENRICHER_PROJECTIONS.exists():
        import pytest

        pytest.skip(f"enricher source not in workspace: {ENRICHER_PROJECTIONS}")
    live = set(_live_map(ENRICHER_PROJECTIONS).values())
    assert live == _snapshot_types(), (
        f"enricher types {sorted(live - _snapshot_types())} are not in the typed query; add them to MB_ARTIST_RELATIONSHIP_TYPES and the snapshot"
    )
