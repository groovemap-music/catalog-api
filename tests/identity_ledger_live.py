"""The works list and density statements as they ran live before the precomputed views.

``api/queries/identity_ledger_queries.py`` read every release item's event history on each
request until gm-catalog-api-gorv (catalog-api 6d744e2). Its works list and density routes now
read database-schema's ``identity_ledger_*`` materialized views. These are the live statements
verbatim, kept only as the reference `tests/test_identity_ledger_integration.py` holds the views'
payloads equal to after ``identity_ledger_refresh()``. Nothing outside the tests runs them.
"""

from __future__ import annotations

from api.queries.identity_ledger_queries import _PARENTS_OF_MEMBER, _RESOLVED


# Per item of one kind, every event its aliases and its MusicBrainz rows' link changes carry:
# its mint, aliases opened later, aliases closed other than by a supersession, and dated link
# changes (`item_id` is NULL when no current alias holds the MusicBrainz row). Supersession
# events are added by each statement, which counts them per side or once.
ITEM_EVENTS = """
kind_alias AS (
    SELECT alias.native_id, alias.provider, alias.external_id, alias.valid_from, alias.valid_to,
           min(alias.valid_from) OVER item AS mint,
           min(alias.valid_from) FILTER (WHERE alias.provider = 'discogs') OVER item AS discogs_at
    FROM provider_aliases AS alias
    JOIN catalog_items AS catalog_item ON catalog_item.id = alias.native_id AND catalog_item.kind = %(kind)s
    WINDOW item AS (PARTITION BY alias.native_id)
),
moved_in AS (
    SELECT survivor_id AS item_id, valid_from AS at FROM catalog_item_supersessions
    UNION
    SELECT superseded_id, valid_to FROM catalog_item_supersessions WHERE valid_to IS NOT NULL
),
moved_out AS (
    SELECT superseded_id AS item_id, valid_from AS at FROM catalog_item_supersessions
    UNION
    SELECT survivor_id, valid_to FROM catalog_item_supersessions WHERE valid_to IS NOT NULL
),
later AS (
    SELECT kind_alias.native_id, kind_alias.provider, kind_alias.valid_from, kind_alias.discogs_at,
           row_number() OVER (
               PARTITION BY kind_alias.native_id, kind_alias.provider = 'musicbrainz'
               ORDER BY kind_alias.valid_from, kind_alias.external_id
           ) AS position
    FROM kind_alias
    LEFT JOIN moved_in ON moved_in.item_id = kind_alias.native_id AND moved_in.at = kind_alias.valid_from
    WHERE kind_alias.valid_from > kind_alias.mint AND moved_in.item_id IS NULL
),
item_event AS (
    SELECT DISTINCT native_id AS item_id, mint AS at, 'minted' AS type, 'groovemap' AS dated_from
    FROM kind_alias
    UNION ALL
    SELECT native_id, valid_from,
           CASE WHEN provider = 'musicbrainz' AND discogs_at IS NOT NULL AND position = 1
                THEN 'linked_at_ingest' ELSE 'evidence_attached' END,
           'groovemap'
    FROM later
    UNION ALL
    SELECT kind_alias.native_id, kind_alias.valid_to, 'alias_closed', 'groovemap'
    FROM kind_alias
    LEFT JOIN moved_out ON moved_out.item_id = kind_alias.native_id AND moved_out.at = kind_alias.valid_to
    WHERE kind_alias.valid_to IS NOT NULL AND moved_out.item_id IS NULL
    UNION ALL
    SELECT holder.native_id, link.event_at, link.event,
           CASE link.dated_from WHEN 'musicbrainz_fullexport' THEN 'musicbrainz_fullexport' ELSE 'groovemap' END
    FROM identity_link_events AS link
    LEFT JOIN provider_aliases AS holder
           ON holder.provider = link.provider AND holder.entity_kind = link.entity_kind
          AND holder.external_id = link.external_id AND holder.valid_to IS NULL
    WHERE link.provider = 'musicbrainz' AND link.entity_kind = %(kind)s
),
supersession_of_kind AS (
    SELECT supersession.*
    FROM catalog_item_supersessions AS supersession
    JOIN catalog_items AS superseded ON superseded.id = supersession.superseded_id AND superseded.kind = %(kind)s
)"""

WORKS = f"""
WITH {ITEM_EVENTS},
side_event AS (
    SELECT item_id, at, type, dated_from FROM item_event WHERE type <> 'minted' AND item_id IS NOT NULL
    UNION ALL
    SELECT side.item_id, supersession.valid_from,
           CASE supersession.cause WHEN 'catalog_reattachment' THEN 'reattached' ELSE 'promoted' END, 'groovemap'
    FROM supersession_of_kind AS supersession
    CROSS JOIN LATERAL (VALUES (supersession.superseded_id), (supersession.survivor_id)) AS side (item_id)
    UNION ALL
    SELECT side.item_id, supersession.valid_to, 'reverted', 'groovemap'
    FROM supersession_of_kind AS supersession
    CROSS JOIN LATERAL (VALUES (supersession.superseded_id), (supersession.survivor_id)) AS side (item_id)
    WHERE supersession.valid_to IS NOT NULL
),
edition_event AS (
    SELECT COALESCE(edition_open.survivor_id, side_event.item_id) AS edition_id, side_event.at, side_event.type
    FROM side_event
    {_RESOLVED.format(alias="edition_open", item="side_event.item_id")}
    WHERE (%(event)s::text IS NULL OR side_event.type = %(event)s::text)
      AND (%(since)s::timestamptz IS NULL OR side_event.at >= %(since)s::timestamptz)
      AND (%(until)s::timestamptz IS NULL OR side_event.at < %(until)s::timestamptz)
),
evented AS (
    SELECT edition_id, min(at) AS first_at, max(at) AS last_at,
           bool_or(type IN ('reattached', 'promoted', 'reverted')) AS repaired
    FROM edition_event
    GROUP BY edition_id
),
member AS MATERIALIZED (
    SELECT edition_id, edition_id AS item_id FROM evented
    UNION
    SELECT supersession.survivor_id, supersession.superseded_id
    FROM catalog_item_supersessions AS supersession
    JOIN evented ON evented.edition_id = supersession.survivor_id
    WHERE supersession.valid_to IS NULL
),
{_PARENTS_OF_MEMBER},
per_work AS (
    SELECT edition_parent.work_id,
           count(DISTINCT edition_parent.edition_id) AS evented_editions,
           count(DISTINCT edition_parent.edition_id) FILTER (WHERE evented.repaired) AS repaired_editions,
           min(evented.first_at) AS first_event,
           max(evented.last_at) AS last_event
    FROM edition_parent
    JOIN evented ON evented.edition_id = edition_parent.edition_id
    GROUP BY edition_parent.work_id
)
SELECT total.works, page.work_id, page.evented_editions, page.repaired_editions, page.first_event, page.last_event
FROM (SELECT count(*) AS works FROM per_work) AS total
LEFT JOIN LATERAL (
    SELECT * FROM per_work ORDER BY last_event DESC, work_id LIMIT %(limit)s OFFSET %(offset)s
) AS page ON TRUE
ORDER BY page.last_event DESC, page.work_id
"""  # noqa: S608 — composed of module constants only

DENSITY = f"""
WITH {ITEM_EVENTS},
event AS (
    SELECT at, type, dated_from FROM item_event
    UNION ALL
    SELECT valid_from, CASE cause WHEN 'catalog_reattachment' THEN 'reattached' ELSE 'promoted' END, 'groovemap'
    FROM supersession_of_kind
    UNION ALL
    SELECT valid_to, 'reverted', 'groovemap' FROM supersession_of_kind WHERE valid_to IS NOT NULL
)
SELECT to_char(at AT TIME ZONE 'UTC', 'YYYY-MM') AS month, type, dated_from, count(*) AS events
FROM event
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""  # noqa: S608 — composed of module constants only
