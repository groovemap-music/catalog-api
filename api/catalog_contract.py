"""Composed adapter over the two source-owned catalog-events producer contracts.

Per ADR 0005, "Source-owned catalog ingestion repositories"
(https://github.com/groovemap-music/design/blob/main/docs/adr/0005-source-owned-catalog-ingestion.md),
`groovemap.catalog-events/v1` is produced independently by `discogs-ingestion` and
`musicbrainz-ingestion`; catalog-api composes both into one module because its admin
router purges dead-letter queues across all four registered consumers. The two producer
contracts and their generated Python bindings are promoted byte-for-byte into
`contracts/catalog-events/v1/discogs/` and `contracts/catalog-events/v1/musicbrainz/`
(see each subdirectory's `source.json`, digest-verified by `scripts/check-contracts.py`).

This module is hand-authored, not generated: the split bindings expose only
`exchange_name(entity)` and `queue_name(consumer, entity)` per source, with no
dead-letter helpers, so this adapter reconstructs `dead_letter_exchange_name` /
`dead_letter_queue_name` with the same `.dlx` / `.dlq` templates the promoted contracts'
`queue` section defines, and re-exports both sources' entity vocabulary and exchange
prefixes under one import. ADR 0005 freezes every runtime AMQP identifier produced here
across a producer promotion; `tests/test_catalog_contract_frozen_identifiers.py` snapshots
them against both promoted contracts' `runtime_identifiers` blocks so a future promotion
that shifts a name is caught immediately.
"""

from __future__ import annotations

from os import getenv

CONTRACT_NAME = "groovemap.catalog-events"
CONTRACT_VERSION = 1
AMQP_EXCHANGE_TYPE = "fanout"
DISCOGS_DATA_TYPES = ["artists", "labels", "masters", "releases"]
MUSICBRAINZ_DATA_TYPES = ["artists", "labels", "release-groups", "releases"]
DISCOGS_EXCHANGE_PREFIX = getenv(
    "DISCOGS_EXCHANGE_PREFIX",
    "groovemap-discogs",
)
MUSICBRAINZ_EXCHANGE_PREFIX = getenv(
    "MUSICBRAINZ_EXCHANGE_PREFIX",
    "groovemap-musicbrainz",
)
CONSUMER_SOURCES = {
    "brainzgraphinator": {"source": "musicbrainz"},
    "brainztableinator": {"source": "musicbrainz"},
    "graphinator": {"source": "discogs"},
    "tableinator": {"source": "discogs"},
}

# Compatibility names used by the current services. They mirror the promoted,
# producer-owned contracts rather than being independently declared here.
DATA_TYPES = DISCOGS_DATA_TYPES
AMQP_QUEUE_PREFIX_GRAPHINATOR = f"{DISCOGS_EXCHANGE_PREFIX}-graphinator"
AMQP_QUEUE_PREFIX_TABLEINATOR = f"{DISCOGS_EXCHANGE_PREFIX}-tableinator"
AMQP_QUEUE_PREFIX_BRAINZGRAPHINATOR = f"{MUSICBRAINZ_EXCHANGE_PREFIX}-brainzgraphinator"
AMQP_QUEUE_PREFIX_BRAINZTABLEINATOR = f"{MUSICBRAINZ_EXCHANGE_PREFIX}-brainztableinator"


def entity_types(source: str) -> list[str]:
    """Return the entity vocabulary for a catalog source."""
    if source == "discogs":
        return DISCOGS_DATA_TYPES
    if source == "musicbrainz":
        return MUSICBRAINZ_DATA_TYPES
    raise ValueError(f"Unknown catalog source: {source}")


def exchange_prefix(source: str) -> str:
    """Return the environment-aware exchange prefix for a source."""
    if source == "discogs":
        return DISCOGS_EXCHANGE_PREFIX
    if source == "musicbrainz":
        return MUSICBRAINZ_EXCHANGE_PREFIX
    raise ValueError(f"Unknown catalog source: {source}")


def exchange_name(source: str, entity: str) -> str:
    """Build a producer-owned exchange name."""
    _require_entity(source, entity)
    return f"{exchange_prefix(source)}-{entity}"


def queue_name(consumer: str, entity: str) -> str:
    """Build a registered consumer queue name."""
    try:
        source = CONSUMER_SOURCES[consumer]["source"]
    except KeyError as exc:
        raise ValueError(f"Unknown catalog consumer: {consumer}") from exc
    _require_entity(source, entity)
    return f"{exchange_prefix(source)}-{consumer}-{entity}"


def dead_letter_exchange_name(consumer: str, entity: str) -> str:
    """Build the dead-letter exchange name for a consumer queue."""
    return f"{queue_name(consumer, entity)}.dlx"


def dead_letter_queue_name(consumer: str, entity: str) -> str:
    """Build the dead-letter queue name for a consumer queue."""
    return f"{queue_name(consumer, entity)}.dlq"


def _require_entity(source: str, entity: str) -> None:
    if entity not in entity_types(source):
        raise ValueError(f"Unknown {source} entity: {entity}")
