"""Frozen-identifier tests for the composed catalog-events adapter.

ADR 0005 (https://github.com/groovemap-music/design/blob/main/docs/adr/
0005-source-owned-catalog-ingestion.md) freezes this service's runtime AMQP
identifiers: a producer-contract promotion must never rename a durable
exchange, queue, dead-letter exchange, or dead-letter queue a consumer
already has messages under. FROZEN_NAMES below is a snapshot of exactly what
``api.catalog_contract`` -- the hand-authored adapter that composes the two
promoted, source-owned producer contracts -- must keep producing for every
registered consumer. Every assertion here checks the adapter, and both
promoted contracts' own ``runtime_identifiers`` blocks, against that frozen
snapshot, so a future promotion that silently shifts a name is caught
immediately.
"""

from __future__ import annotations

import json
from pathlib import Path

from api.catalog_contract import (
    DATA_TYPES,
    MUSICBRAINZ_DATA_TYPES,
    dead_letter_exchange_name,
    dead_letter_queue_name,
    exchange_name,
    queue_name,
)


ROOT = Path(__file__).parent.parent

# Snapshot captured from the promoted discogs-ingestion and musicbrainz-ingestion
# contracts. Do not update these values to match a new promotion -- a change here
# means a durable AMQP identifier moved.
FROZEN_NAMES: dict[str, dict[str, dict[str, str]]] = {
    "discogs": {
        entity: {
            "exchange": f"groovemap-discogs-{entity}",
            "consumers": {
                consumer: {
                    "queue": f"groovemap-discogs-{consumer}-{entity}",
                    "dead_letter_exchange": f"groovemap-discogs-{consumer}-{entity}.dlx",
                    "dead_letter_queue": f"groovemap-discogs-{consumer}-{entity}.dlq",
                }
                for consumer in ("graphinator", "tableinator")
            },
        }
        for entity in ("artists", "labels", "masters", "releases")
    },
    "musicbrainz": {
        entity: {
            "exchange": f"groovemap-musicbrainz-{entity}",
            "consumers": {
                consumer: {
                    "queue": f"groovemap-musicbrainz-{consumer}-{entity}",
                    "dead_letter_exchange": f"groovemap-musicbrainz-{consumer}-{entity}.dlx",
                    "dead_letter_queue": f"groovemap-musicbrainz-{consumer}-{entity}.dlq",
                }
                for consumer in ("brainzgraphinator", "brainztableinator")
            },
        }
        for entity in ("artists", "labels", "release-groups", "releases")
    },
}


def test_frozen_names_cover_every_registered_entity_and_consumer() -> None:
    assert set(FROZEN_NAMES["discogs"]) == set(DATA_TYPES)
    assert set(FROZEN_NAMES["musicbrainz"]) == set(MUSICBRAINZ_DATA_TYPES)
    assert {c for entity in FROZEN_NAMES["discogs"].values() for c in entity["consumers"]} == {
        "graphinator",
        "tableinator",
    }
    assert {c for entity in FROZEN_NAMES["musicbrainz"].values() for c in entity["consumers"]} == {
        "brainzgraphinator",
        "brainztableinator",
    }


def test_adapter_reproduces_the_frozen_identifiers() -> None:
    for source, entities in FROZEN_NAMES.items():
        for entity, expected in entities.items():
            assert exchange_name(source, entity) == expected["exchange"]
            for consumer, names in expected["consumers"].items():
                assert queue_name(consumer, entity) == names["queue"]
                assert dead_letter_exchange_name(consumer, entity) == names["dead_letter_exchange"]
                assert dead_letter_queue_name(consumer, entity) == names["dead_letter_queue"]


def test_frozen_identifiers_match_the_promoted_contracts_runtime_identifiers() -> None:
    for source, entities in FROZEN_NAMES.items():
        contract = json.loads((ROOT / "contracts/catalog-events/v1" / source / "contract.json").read_text())
        runtime_identifiers = contract["runtime_identifiers"]

        for entity, expected in entities.items():
            assert runtime_identifiers["exchanges"][entity] == expected["exchange"]
            for consumer, names in expected["consumers"].items():
                queue = runtime_identifiers["queues"][consumer][entity]
                assert queue["name"] == names["queue"]
                assert queue["dead_letter_exchange"] == names["dead_letter_exchange"]
                assert queue["dead_letter_queue"] == names["dead_letter_queue"]
