-- Deterministic synthetic catalog at gm-design-loj9's replayed scale, for the Identity Ledger
-- routes (scripts/ledger_scale/README.md). Run with psql against a database the pinned
-- database-schema has been applied to:
--
--     psql -v scale=1.0 -f scripts/ledger_scale/seed.sql
--
-- Every row is a function of its position: no random() and no clock, so two runs at one scale
-- write byte-identical tables. Rows are generated inside PostgreSQL (generate_series over
-- `ledger_scale.gen_*` views) and written with COPY ... FREEZE from a psql inside the server
-- container, so nothing crosses the client connection and nothing is vacuumed afterwards.
--
-- The counts are loj9's (design docs/spikes/gm-design-loj9-strata-ledger.md, sections 2, 3
-- and 5), multiplied by `scale`. Nothing here is Discogs or MusicBrainz data.

\set ON_ERROR_STOP on
\if :{?scale}
\else
\set scale 1.0
\endif

SET client_min_messages = warning;
SET maintenance_work_mem = '1GB';
SET max_parallel_maintenance_workers = 4;

DROP SCHEMA IF EXISTS ledger_scale CASCADE;
CREATE SCHEMA ledger_scale;

-- ---------------------------------------------------------------------------------------------
-- The model: loj9's two cycles, one month apart.
--
--   t1  2026-09-01 02:00  Discogs load, month 1   (every record mints an item)
--   t2  2026-09-02 02:00  MusicBrainz load, month 1 (a row whose Discogs id resolves attaches
--                          to that item: linked at ingest; any other row mints its own item)
--       R1 writes nothing, so it has no rows.
--   t4  2026-10-01 02:00  Discogs load, month 2   (new records mint)
--   t5  2026-10-02 02:00  MusicBrainz load, month 2 (new rows attach or mint; rows whose link
--                          was added or changed write a `groovemap_load` link event)
--   t6  2026-10-02 20:00  R2: each split row's alias closes on its own item and reopens on the
--                          Discogs item at one instant, and one supersession opens
--
-- A load commits 1,000 rows per transaction, one second apart, in kind order (artist, label,
-- master, release); a reload walks every record of the dump, and only new ones mint.
-- ---------------------------------------------------------------------------------------------

CREATE TABLE ledger_scale.setting (scale numeric NOT NULL);
INSERT INTO ledger_scale.setting VALUES (:scale);

-- Per kind, at scale 1 (loj9 section 2, release groups as `master`):
--   d1, d2   Discogs records minted in month 1 and month 2
--   l1, o1   MusicBrainz rows linked at ingest and minted as their own item at t2
--   l2, o2   the same at t5
--   a, k, c  rows R2 re-attaches: Discogs record arrived at t4, link added at t5, link changed at t5
--   u        rows still unresolved after R2 (they name a Discogs release no dump holds)
--   dup      Discogs releases named by a second MusicBrainz release (loj9 section 5: 17,085 of
--            1,879,464 linked, scaled to the replay's 1,881,444)
-- Month-1 Discogs counts are section 3's alias totals less the MusicBrainz rows of month 1.
CREATE TABLE ledger_scale.plan (
    kind   text PRIMARY KEY,
    kcode  int NOT NULL,
    d1     bigint NOT NULL, d2 bigint NOT NULL,
    l1     bigint NOT NULL, o1 bigint NOT NULL,
    l2     bigint NOT NULL, o2 bigint NOT NULL,
    a      bigint NOT NULL, k bigint NOT NULL, c bigint NOT NULL, u bigint NOT NULL,
    dup    bigint NOT NULL,
    off_t1 bigint NOT NULL, off_t2 bigint NOT NULL, off_t4 bigint NOT NULL, off_t5 bigint NOT NULL, off_t6 bigint NOT NULL
);

INSERT INTO ledger_scale.plan
SELECT kind, kcode, d1, d2, l1, o1, l2, o2, a, k, c, u, dup,
       coalesce(sum(d1) OVER earlier, 0),
       coalesce(sum(l1 + o1) OVER earlier, 0),
       coalesce(sum(d1 + d2) OVER earlier, 0),
       coalesce(sum(l1 + o1 + l2 + o2) OVER earlier, 0),
       coalesce(sum(a + k + c) OVER earlier, 0)
FROM (
    SELECT kind, kcode,
           round(d1 * s)::bigint AS d1, round(d2 * s)::bigint AS d2,
           round(l1 * s)::bigint AS l1, round(o1 * s)::bigint AS o1,
           round(l2 * s)::bigint AS l2, round(o2 * s)::bigint AS o2,
           round(a * s)::bigint AS a, round(k * s)::bigint AS k, round(c * s)::bigint AS c, round(u * s)::bigint AS u,
           round(dup * s)::bigint AS dup
    FROM ledger_scale.setting AS setting (s)
    CROSS JOIN (VALUES
        ('artist',  1, 10189040, 46171, 1263156, 1752530,  723, 3159, 203, 416, 2,    0,     0),
        ('label',   2,  2410384, 11974,  163539,  189825,   75,  319,  11,  33, 0,    0,     0),
        ('master',  3,  2589349,  9639,  348399, 4235510,   38, 8057,  97, 240, 0,    0,     0),
        ('release', 4, 19417067, 78951, 1881444, 3976654, 1315, 8678, 682, 522, 0, 6607, 17103)
    ) AS counts (kind, kcode, d1, d2, l1, o1, l2, o2, a, k, c, u, dup)
) AS scaled
WINDOW earlier AS (ORDER BY kcode ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING);

-- How each linked edition's two catalogs name its work (loj9 section 5), over the editions a
-- linked MusicBrainz release makes. Two parents: the release group names no master but the
-- release has one, or they name different masters. Their sum is the replay's 564,667; the
-- one-parent cases split the rest in section 5's shares.
CREATE TABLE ledger_scale.parents (
    np            bigint NOT NULL,  -- linked editions: one per primary row of l1, plus l2
    same          bigint NOT NULL,
    rg_no_master  bigint NOT NULL,
    different     bigint NOT NULL,
    rel_no_master bigint NOT NULL,
    no_parent     bigint NOT NULL,  -- Discogs-only editions with no master (loj9: 7,453,079)
    p_master      float8 NOT NULL   -- share of the other Discogs-only releases that have a master
);

INSERT INTO ledger_scale.parents
SELECT np, same, two - different, different, rel_no_master, no_parent,
       (unlinked - no_parent)::float8 / unlinked
FROM (
    SELECT np, two, different, no_parent, unlinked,
           round((np - two) * 653532.0 / 1312789)::bigint AS same,
           round((np - two) * 2653.0 / 1312789)::bigint AS rel_no_master
    FROM (
        SELECT release.l1 - release.dup + release.l2 AS np,
               round(564667 * s)::bigint AS two,
               round(8553 * s)::bigint AS different,
               round(7453079 * s)::bigint AS no_parent,
               release.d1 + release.d2 - (release.l1 - release.dup) - release.l2 - release.a - release.k AS unlinked
        FROM ledger_scale.plan AS release, ledger_scale.setting AS setting (s)
        WHERE release.kind = 'release'
    ) AS base
) AS shares;

-- ---------------------------------------------------------------------------------------------
-- Pure functions of position.
-- ---------------------------------------------------------------------------------------------

-- A load step's instant for its pos-th row: 1,000 rows per transaction, one second apart.
CREATE FUNCTION ledger_scale.at(start timestamptz, pos bigint) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN start + (pos / 1000) * interval '1 second';

-- A UUIDv7 whose 48-bit time is `at` and whose remaining bits are a tag and a counter, so ids
-- sort by mint time as `uuidv7()` ones do, and are reproducible.
CREATE FUNCTION ledger_scale.id(at timestamptz, tag int, n bigint) RETURNS uuid
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN (lpad(to_hex(floor(extract(epoch FROM at) * 1000)::bigint), 12, '0') || '7'
            || lpad(to_hex(tag), 3, '0') || '8' || lpad(to_hex(n), 15, '0'))::uuid;

-- A uniform [0, 1) value per (n, salt), from PostgreSQL's stable 64-bit integer hash.
CREATE FUNCTION ledger_scale.u(n bigint, salt bigint) RETURNS float8
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN (hashint8extended(n, salt) & 140737488355327)::float8 / 140737488355328.0;

-- An index in [0, size) weighted towards 0: a few heavy masters and release groups, a long tail.
CREATE FUNCTION ledger_scale.skew(n bigint, salt bigint, size bigint, power float8) RETURNS bigint
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN least(size - 1, floor(size * ledger_scale.u(n, salt) ^ power)::bigint);

CREATE FUNCTION ledger_scale.dg_at(p ledger_scale.plan, n bigint) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN CASE WHEN n <= p.d1 THEN ledger_scale.at('2026-09-01 02:00:00+00', p.off_t1 + n - 1)
                ELSE ledger_scale.at('2026-10-01 02:00:00+00', p.off_t4 + n - 1) END;

-- The item Discogs record n of a kind minted.
CREATE FUNCTION ledger_scale.dg_item(p ledger_scale.plan, n bigint) RETURNS uuid
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN ledger_scale.id(ledger_scale.dg_at(p, n), p.kcode * 16 + 1, n);

-- When MusicBrainz row m was first written (minted or attached), and when the t5 reload wrote it.
CREATE FUNCTION ledger_scale.mb_at(p ledger_scale.plan, m bigint) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN CASE WHEN m <= p.l1 + p.o1 THEN ledger_scale.at('2026-09-02 02:00:00+00', p.off_t2 + m - 1)
                ELSE ledger_scale.at('2026-10-02 02:00:00+00', p.off_t5 + m - 1) END;

CREATE FUNCTION ledger_scale.t5_at(p ledger_scale.plan, m bigint) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN ledger_scale.at('2026-10-02 02:00:00+00', p.off_t5 + m - 1);

-- R2 re-attaches one item at a time, 10 ms apart.
CREATE FUNCTION ledger_scale.t6_at(p ledger_scale.plan, r bigint) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN timestamptz '2026-10-02 20:00:00+00' + (p.off_t6 + r) * interval '10 milliseconds';

CREATE FUNCTION ledger_scale.mb_item(p ledger_scale.plan, m bigint) RETURNS uuid
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN ledger_scale.id(ledger_scale.mb_at(p, m), p.kcode * 16 + 2, m);

-- MBIDs are random in MusicBrainz; an md5 of (kind, row) stands in.
CREATE FUNCTION ledger_scale.mbid(p ledger_scale.plan, m bigint) RETURNS uuid
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN md5(p.kind || ':' || m)::uuid;

-- The Discogs record the j-th distinct linked row names: spread evenly over month 1.
CREATE FUNCTION ledger_scale.target(p ledger_scale.plan, j bigint) RETURNS bigint
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN 1 + (j * p.d1) / (p.l1 - p.dup);

-- The distinct linked row a row linked at ingest stands for: itself, or for a second
-- MusicBrainz row on one Discogs release, the row it duplicates.
CREATE FUNCTION ledger_scale.primary_of(p ledger_scale.plan, i bigint) RETURNS bigint
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN CASE WHEN i < p.l1 - p.dup THEN i ELSE (i - (p.l1 - p.dup)) * ((p.l1 - p.dup) / p.dup) END;

-- The Discogs record row m's alias is on today, or NULL for a row on its own item.
-- Rows of o1, in order: a (arrived), k (link added), c (link changed), u (unresolved), the rest.
CREATE FUNCTION ledger_scale.mb_link(p ledger_scale.plan, m bigint) RETURNS bigint
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN CASE
        WHEN m <= p.l1 THEN ledger_scale.target(p, ledger_scale.primary_of(p, m - 1))
        WHEN m > p.l1 + p.o1 + p.l2 THEN NULL
        WHEN m > p.l1 + p.o1 THEN p.d1 + p.a + 1 + ((m - p.l1 - p.o1 - 1) * (p.d2 - p.a)) / p.l2
        WHEN m - p.l1 - 1 < p.a THEN p.d1 + 1 + (m - p.l1 - 1)
        WHEN m - p.l1 - 1 < p.a + p.k
            THEN ledger_scale.target(p, ((m - p.l1 - 1 - p.a) * (p.l1 - p.dup)) / p.k) + 1
        WHEN m - p.l1 - 1 < p.a + p.k + p.c
            THEN ledger_scale.target(p, ((m - p.l1 - 1 - p.a - p.k) * (p.l1 - p.dup)) / p.c) + 2
    END;

-- The Discogs id row m names today: its link, or for an unresolved row an id no dump holds.
CREATE FUNCTION ledger_scale.mb_named(p ledger_scale.plan, m bigint) RETURNS bigint
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN coalesce(
        ledger_scale.mb_link(p, m),
        CASE WHEN m > p.l1 AND m - p.l1 - 1 >= p.a + p.k + p.c AND m - p.l1 - 1 < p.a + p.k + p.c + p.u
             THEN p.d1 + p.d2 + 1000 + (m - p.l1 - 1 - p.a - p.k - p.c) END
    );

-- Section 5's case for linked edition x (a primary row of l1, then l2): an exact count of each,
-- spread by a multiplicative permutation of [0, np).
CREATE FUNCTION ledger_scale.parent_case(w ledger_scale.parents, x bigint) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN CASE
        WHEN (x * 2654435761) % w.np < w.same THEN 'same'
        WHEN (x * 2654435761) % w.np < w.same + w.rg_no_master THEN 'rg_no_master'
        WHEN (x * 2654435761) % w.np < w.same + w.rg_no_master + w.different THEN 'different'
        WHEN (x * 2654435761) % w.np < w.same + w.rg_no_master + w.different + w.rel_no_master THEN 'rel_no_master'
        ELSE 'neither'
    END;

-- A linked release group (a row of the master kind's l1) for linked edition x.
CREATE FUNCTION ledger_scale.linked_rg(g ledger_scale.plan, x bigint) RETURNS bigint
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN ledger_scale.skew(x, 11, g.l1, 1.5);

-- A release group on its own item, past the ones R2 re-attaches or leaves unresolved.
CREATE FUNCTION ledger_scale.own_rg(g ledger_scale.plan, n bigint, salt bigint) RETURNS bigint
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN g.l1 + 1 + g.a + g.k + g.c + g.u + ledger_scale.skew(n, salt, g.o1 - g.a - g.k - g.c - g.u, 1.2);

-- A Discogs master for a release that has one and no linked edition fixing it.
CREATE FUNCTION ledger_scale.any_master(g ledger_scale.plan, n bigint) RETURNS bigint
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN 1 + ledger_scale.skew(n, 17, g.d1, 1.5);

-- ---------------------------------------------------------------------------------------------
-- Generators, one view per table, each in the order the loads would have written it.
-- ---------------------------------------------------------------------------------------------

CREATE VIEW ledger_scale.gen_catalog_items AS
SELECT ledger_scale.dg_item(p, n) AS id, p.kind, ledger_scale.dg_at(p, n) AS created_at
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(1, p.d1) AS n
UNION ALL
SELECT ledger_scale.mb_item(p, m), p.kind, ledger_scale.mb_at(p, m)
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(p.l1 + 1, p.l1 + p.o1) AS m
UNION ALL
SELECT ledger_scale.dg_item(p, n), p.kind, ledger_scale.dg_at(p, n)
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(p.d1 + 1, p.d1 + p.d2) AS n
UNION ALL
SELECT ledger_scale.mb_item(p, m), p.kind, ledger_scale.mb_at(p, m)
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(p.l1 + p.o1 + p.l2 + 1, p.l1 + p.o1 + p.l2 + p.o2) AS m;

-- Columns: id, provider, entity_kind, external_id, native_id, valid_from, valid_to, confidence,
-- source, asserted_at.
CREATE VIEW ledger_scale.gen_provider_aliases AS
SELECT ledger_scale.id(ledger_scale.dg_at(p, n), p.kcode * 16 + 3, n) AS id, 'discogs' AS provider, p.kind AS entity_kind,
       n::text AS external_id, ledger_scale.dg_item(p, n) AS native_id, ledger_scale.dg_at(p, n) AS valid_from,
       NULL::timestamptz AS valid_to, 1.0::real AS confidence, 'catalog' AS source, ledger_scale.dg_at(p, n) AS asserted_at
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(1, p.d1) AS n
UNION ALL
SELECT ledger_scale.id(ledger_scale.mb_at(p, m), p.kcode * 16 + 4, m), 'musicbrainz', p.kind,
       ledger_scale.mbid(p, m)::text,
       CASE WHEN m <= p.l1 THEN ledger_scale.dg_item(p, ledger_scale.mb_link(p, m)) ELSE ledger_scale.mb_item(p, m) END,
       ledger_scale.mb_at(p, m),
       CASE WHEN m > p.l1 AND m - p.l1 - 1 < p.a + p.k + p.c THEN ledger_scale.t6_at(p, m - p.l1 - 1) END,
       1.0, 'catalog', ledger_scale.mb_at(p, m)
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(1, p.l1 + p.o1) AS m
UNION ALL
SELECT ledger_scale.id(ledger_scale.dg_at(p, n), p.kcode * 16 + 3, n), 'discogs', p.kind, n::text,
       ledger_scale.dg_item(p, n), ledger_scale.dg_at(p, n), NULL, 1.0, 'catalog', ledger_scale.dg_at(p, n)
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(p.d1 + 1, p.d1 + p.d2) AS n
UNION ALL
SELECT ledger_scale.id(ledger_scale.mb_at(p, m), p.kcode * 16 + 4, m), 'musicbrainz', p.kind, ledger_scale.mbid(p, m)::text,
       CASE WHEN m <= p.l1 + p.o1 + p.l2 THEN ledger_scale.dg_item(p, ledger_scale.mb_link(p, m)) ELSE ledger_scale.mb_item(p, m) END,
       ledger_scale.mb_at(p, m), NULL, 1.0, 'catalog', ledger_scale.mb_at(p, m)
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(p.l1 + p.o1 + 1, p.l1 + p.o1 + p.l2 + p.o2) AS m
UNION ALL
-- R2 reopens each re-attached row's alias on the Discogs item at the supersession's instant.
SELECT ledger_scale.id(ledger_scale.t6_at(p, r), p.kcode * 16 + 5, r), 'musicbrainz', p.kind,
       ledger_scale.mbid(p, p.l1 + 1 + r)::text, ledger_scale.dg_item(p, ledger_scale.mb_link(p, p.l1 + 1 + r)),
       ledger_scale.t6_at(p, r), NULL, 1.0, 'catalog', ledger_scale.t6_at(p, r)
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(0, p.a + p.k + p.c - 1) AS r;

-- Columns: id, superseded_id, survivor_id, cause, decision_ref, valid_from, valid_to, via_id.
CREATE VIEW ledger_scale.gen_catalog_item_supersessions AS
SELECT ledger_scale.id(ledger_scale.t6_at(p, r), p.kcode * 16 + 6, r) AS id,
       ledger_scale.mb_item(p, p.l1 + 1 + r) AS superseded_id,
       ledger_scale.dg_item(p, ledger_scale.mb_link(p, p.l1 + 1 + r)) AS survivor_id,
       'catalog_reattachment' AS cause,
       ledger_scale.id('2026-10-02 20:00:00+00', 4095, 2) AS decision_ref,
       ledger_scale.t6_at(p, r) AS valid_from, NULL::timestamptz AS valid_to, NULL::uuid AS via_id
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(0, p.a + p.k + p.c - 1) AS r;

-- Columns: id, provider, entity_kind, external_id, event, discogs_id_before, discogs_id_after,
-- event_at, dated_from, source_ref, decision_ref, recorded_at.
--
-- The backfill dates one `link_added` per MusicBrainz release that named a Discogs release in
-- the export (the rows linked at ingest, and the arrived and unresolved ones), from mid-2004 to
-- the first load, denser towards the present. The t5 load records the rows whose link was
-- added or changed.
CREATE VIEW ledger_scale.gen_identity_link_events AS
SELECT ledger_scale.id(ledger_scale.at('2026-09-05 12:00:00+00', m - 1), p.kcode * 16 + 7, m) AS id,
       'musicbrainz' AS provider, p.kind AS entity_kind, ledger_scale.mbid(p, m)::text AS external_id,
       'link_added' AS event, NULL::bigint AS discogs_id_before, ledger_scale.mb_named(p, m) AS discogs_id_after,
       date_trunc('second', timestamptz '2004-06-01 00:00:00+00'
           + ledger_scale.u(m, 23) ^ 0.625 * (timestamptz '2026-09-01 00:00:00+00' - timestamptz '2004-06-01 00:00:00+00')) AS event_at,
       'musicbrainz_fullexport' AS dated_from, '20260904-001001' AS source_ref,
       ledger_scale.id('2026-09-05 12:00:00+00', 4095, 1) AS decision_ref,
       ledger_scale.at('2026-09-05 12:00:00+00', m - 1) AS recorded_at
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(1, p.l1 + p.o1) AS m
WHERE p.kind = 'release'
  AND (m <= p.l1 OR m - p.l1 - 1 < p.a OR (m - p.l1 - 1 >= p.a + p.k + p.c AND m - p.l1 - 1 < p.a + p.k + p.c + p.u))
UNION ALL
SELECT ledger_scale.id(ledger_scale.t5_at(p, m), p.kcode * 16 + 8, m), 'musicbrainz', p.kind, ledger_scale.mbid(p, m)::text,
       CASE WHEN m - p.l1 - 1 < p.a + p.k THEN 'link_added' ELSE 'link_changed' END,
       CASE WHEN m - p.l1 - 1 < p.a + p.k THEN NULL ELSE p.d1 + p.d2 + 500000 + (m - p.l1 - 1 - p.a - p.k) END,
       ledger_scale.mb_link(p, m),
       ledger_scale.t5_at(p, m), 'groovemap_load', '20261002-001001', NULL::uuid, ledger_scale.t5_at(p, m)
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(p.l1 + 1 + p.a, p.l1 + p.a + p.k + p.c) AS m;

-- One edition per linked primary row: its Discogs release, and section 5's case.
CREATE VIEW ledger_scale.linked_edition AS
SELECT x, ledger_scale.target(p, x) AS n, ledger_scale.parent_case(w, x) AS parent_case
FROM ledger_scale.plan AS p CROSS JOIN ledger_scale.parents AS w CROSS JOIN LATERAL generate_series(0, p.l1 - p.dup - 1) AS x
WHERE p.kind = 'release'
UNION ALL
SELECT p.l1 - p.dup + l, p.d1 + p.a + 1 + (l * (p.d2 - p.a)) / p.l2, ledger_scale.parent_case(w, p.l1 - p.dup + l)
FROM ledger_scale.plan AS p CROSS JOIN ledger_scale.parents AS w CROSS JOIN LATERAL generate_series(0, p.l2 - 1) AS l
WHERE p.kind = 'release';

-- Columns: mbid, name, release_group_mbid, discogs_release_id, created_at, updated_at.
CREATE VIEW ledger_scale.gen_musicbrainz_releases AS
SELECT ledger_scale.mbid(p, m) AS mbid, 'synthetic' AS name,
       md5('master:' || CASE
           WHEN m <= p.l1 OR (m > p.l1 + p.o1 AND m <= p.l1 + p.o1 + p.l2) THEN
               CASE WHEN ledger_scale.parent_case(w, x.x) IN ('same', 'different', 'rel_no_master')
                    THEN 1 + ledger_scale.linked_rg(g, x.x)
                    ELSE ledger_scale.own_rg(g, x.x, 13) END
           ELSE ledger_scale.own_rg(g, m, 19)
       END)::uuid AS release_group_mbid,
       ledger_scale.mb_named(p, m) AS discogs_release_id,
       ledger_scale.mb_at(p, m) AS created_at, ledger_scale.mb_at(p, m) AS updated_at
FROM ledger_scale.plan AS p
JOIN ledger_scale.plan AS g ON g.kind = 'master'
CROSS JOIN ledger_scale.parents AS w
CROSS JOIN LATERAL generate_series(1, p.l1 + p.o1 + p.l2 + p.o2) AS m
CROSS JOIN LATERAL (SELECT CASE WHEN m <= p.l1 THEN ledger_scale.primary_of(p, m - 1)
                                ELSE p.l1 - p.dup + (m - p.l1 - p.o1 - 1) END AS x) AS x
WHERE p.kind = 'release';

-- Discogs releases that a linked edition, an arrival, or an added link fixes; every other one
-- has a master with probability p_master, so the editions with no work parent come to loj9's.
CREATE TABLE ledger_scale.fixed_release (n bigint PRIMARY KEY);

-- Columns: release_id, master_id.
CREATE VIEW ledger_scale.gen_derived_from AS
SELECT e.n::text AS release_id,
       (CASE e.parent_case
            WHEN 'same' THEN ledger_scale.target(g, ledger_scale.linked_rg(g, e.x))
            WHEN 'different' THEN ledger_scale.target(g, (ledger_scale.linked_rg(g, e.x) + 1) % g.l1)
            ELSE ledger_scale.any_master(g, e.n)
        END)::text AS master_id
FROM ledger_scale.linked_edition AS e
JOIN ledger_scale.plan AS g ON g.kind = 'master'
WHERE e.parent_case IN ('same', 'different', 'rg_no_master')
UNION ALL
SELECT release.n::text, ledger_scale.any_master(g, release.n)::text
FROM ledger_scale.plan AS p
JOIN ledger_scale.plan AS g ON g.kind = 'master'
CROSS JOIN ledger_scale.parents AS w
CROSS JOIN LATERAL generate_series(1, p.d1 + p.d2) AS release (n)
WHERE p.kind = 'release'
  AND ledger_scale.u(release.n, 29) < w.p_master
  AND NOT EXISTS (SELECT 1 FROM ledger_scale.fixed_release AS fixed WHERE fixed.n = release.n);

-- Columns: data_id, hash, data, updated_at. The ledger reads only the key.
CREATE VIEW ledger_scale.gen_releases AS
SELECT n::text AS data_id, 'synthetic' AS hash, '{}'::jsonb AS data, ledger_scale.dg_at(p, n) AS updated_at
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(1, p.d1 + p.d2) AS n
WHERE p.kind = 'release';

-- ---------------------------------------------------------------------------------------------
-- Load. Constraints and indexes come off first and are rebuilt from their own definitions after
-- each table is filled, except the few no ledger statement can read (`rebuilt = false`), which
-- would only cost disk: provider_aliases' surrogate key, public.releases' document indexes, and
-- musicbrainz.releases' gm_item_id and media indexes.
-- ---------------------------------------------------------------------------------------------

CREATE TABLE ledger_scale.seeded (rel regclass PRIMARY KEY, gen text NOT NULL, columns text NOT NULL, ord int NOT NULL);
INSERT INTO ledger_scale.seeded VALUES
    ('public.provider_aliases', 'gen_provider_aliases',
     'id, provider, entity_kind, external_id, native_id, valid_from, valid_to, confidence, source, asserted_at', 1),
    ('public.catalog_items', 'gen_catalog_items', 'id, kind, created_at', 2),
    ('public.catalog_item_supersessions', 'gen_catalog_item_supersessions',
     'id, superseded_id, survivor_id, cause, decision_ref, valid_from, valid_to, via_id', 3),
    ('public.identity_link_events', 'gen_identity_link_events',
     'id, provider, entity_kind, external_id, event, discogs_id_before, discogs_id_after, event_at, dated_from, source_ref, decision_ref, recorded_at', 4),
    ('musicbrainz.releases', 'gen_musicbrainz_releases', 'mbid, name, release_group_mbid, discogs_release_id, created_at, updated_at', 5),
    ('graph.derived_from', 'gen_derived_from', 'release_id, master_id', 6),
    ('public.releases', 'gen_releases', 'data_id, hash, data, updated_at', 7);

CREATE TABLE ledger_scale.deferred AS
SELECT 1 AS phase, c.conrelid::regclass AS rel, c.conname::text AS name,
       format('ALTER TABLE %s DROP CONSTRAINT %I', c.conrelid::regclass, c.conname) AS drop_sql,
       format('ALTER TABLE %s ADD CONSTRAINT %I %s', c.conrelid::regclass, c.conname, pg_get_constraintdef(c.oid)) AS create_sql
FROM pg_constraint AS c
WHERE c.contype = 'f'
  AND (c.conrelid IN (SELECT rel FROM ledger_scale.seeded) OR c.confrelid IN (SELECT rel FROM ledger_scale.seeded))
UNION ALL
SELECT 2, c.conrelid::regclass, c.conname::text,
       format('ALTER TABLE %s DROP CONSTRAINT %I', c.conrelid::regclass, c.conname),
       format('ALTER TABLE %s ADD CONSTRAINT %I %s', c.conrelid::regclass, c.conname, pg_get_constraintdef(c.oid))
FROM pg_constraint AS c
WHERE c.contype IN ('p', 'u') AND c.conrelid IN (SELECT rel FROM ledger_scale.seeded)
UNION ALL
SELECT 3, i.indrelid::regclass, i.indexrelid::regclass::text,
       format('DROP INDEX %s', i.indexrelid::regclass), pg_get_indexdef(i.indexrelid)
FROM pg_index AS i
WHERE i.indrelid IN (SELECT rel FROM ledger_scale.seeded)
  AND NOT EXISTS (SELECT 1 FROM pg_constraint AS c WHERE c.conindid = i.indexrelid);

ALTER TABLE ledger_scale.deferred ADD COLUMN rebuilt boolean;
UPDATE ledger_scale.deferred
SET rebuilt = NOT (
    (rel = 'public.provider_aliases'::regclass AND name = 'provider_aliases_pkey')
    OR (rel = 'public.releases'::regclass AND phase = 3)
    OR (rel = 'musicbrainz.releases'::regclass AND name IN ('musicbrainz.idx_mb_releases_gm_item_id', 'musicbrainz.idx_mb_releases_media_families'))
);

SELECT drop_sql FROM ledger_scale.deferred ORDER BY phase, rel::text, name \gexec

-- The derived_from generator anti-joins the releases a linked edition fixes.
INSERT INTO ledger_scale.fixed_release
SELECT n FROM ledger_scale.linked_edition
UNION
SELECT ledger_scale.mb_link(p, m)
FROM ledger_scale.plan AS p CROSS JOIN LATERAL generate_series(p.l1 + 1, p.l1 + p.a + p.k) AS m
WHERE p.kind = 'release';
ANALYZE ledger_scale.fixed_release;

-- Each table: truncate, COPY ... FREEZE from its generator (one transaction, so no WAL and no
-- vacuum), rebuild its keys and indexes, then analyze.
SELECT format(
           $load$BEGIN;
TRUNCATE %1$s;
COPY %1$s (%2$s) FROM PROGRAM %3$L WITH (FREEZE);
COMMIT;$load$,
           s.rel, s.columns,
           format('PGOPTIONS=''-c work_mem=256MB'' psql -X -q -v ON_ERROR_STOP=1 -U %s -d %s -c "COPY (SELECT * FROM ledger_scale.%s) TO STDOUT"',
                  current_user, current_database(), s.gen))
FROM ledger_scale.seeded AS s
ORDER BY s.ord \gexec

SELECT d.create_sql
FROM ledger_scale.deferred AS d JOIN ledger_scale.seeded AS s ON s.rel = d.rel
WHERE d.phase IN (2, 3) AND d.rebuilt
ORDER BY s.ord, d.phase, d.name \gexec

SELECT create_sql FROM ledger_scale.deferred WHERE phase = 1 ORDER BY rel::text, name \gexec

TRUNCATE ledger_scale.fixed_release;
ANALYZE public.provider_aliases, public.catalog_items, public.catalog_item_supersessions, public.identity_link_events,
        musicbrainz.releases, graph.derived_from, public.releases;
