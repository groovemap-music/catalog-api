# Explore-from-here discoveries

`GET /api/recommend/explore/{entity_type}/{entity_id}` walks the graph from an entity and ranks what it finds
against the caller's taste. Audit H2 (`gm-catalog-api-zazs`): the old statement enumerated every path before it
applied `LIMIT 100`; Brass & Military took 7.4 s and 463 MB of the 3 GiB shared pool, and Rock was never run
because it would have exhausted it. This page says what the route returns now and what it still leaves out.

## What is returned

| Kind | Returned | Order |
| --- | --- | --- |
| Genres and styles | **All** within `hops`, with shortest distance and path. Never sampled or dropped | distance, type, name |
| Artists and labels | The nearest **100**, as before | distance, then most established first, then id |

Nothing is cut beyond the 100-artist-and-label limit the route already had. "Most established" means
`release_count` (stored on the label) for a label and the number of credits for an artist, which has no stored
count. Before, the 100 were whichever the planner met first among equals, so they changed between runs; now they
are stable. **This is a bias toward well-known names** at a given distance, by design. One consequence to know
about: nameless placeholder artists such as "Various" (id 194) and "Unknown Artist" (id 355) have the most credits
and so lead the artist list for most genres.

Genres and styles are complete because they are few (about 15 genres and 740 styles) and are what the taste score
rewards: a blind spot is boosted 1.5x, and a blind-spot genre must not be missed because 100 artists came first.

Paths are real shortest paths and deterministic (equal-length ties break by name). A path through a hub entity
shows one linking release, the alphabetically first by title.

## How it is bounded

* A start with fewer than 50,000 releases (and every artist, release and master) is expanded breadth first over
  distinct nodes, one query per hop, each edge visited once. It never enumerates paths, and the last hop follows
  only the edges that can still reach an eligible node. Bowie, `hops=3`: about 0.6M db hits.
* A **hub** genre, style or label (`release_count` of 50,000 or more: Rock, Pop Rock, Columbia) cannot be walked
  in a request. Its answer is computed exactly in the background and stored in memory and Valkey
  (`explore-hub:v1:*`, no expiry). Requests read the stored result with no graph access.
* For a hub, `hops=3` equals `hops=2`: the only eligible nodes three steps away are alias and member artists of
  artists already at distance 2, and a hub has far more than 100 of those. `hops=1` is empty, as before: a hub's
  neighbours one step away are releases, which are never discoveries.
* Until a hub is computed (a cold start; about a minute per large hub) its request answers `503` with
  `Retry-After`. A hub whose release count changed is recomputed on the next hourly check, or after 7 days.
* Every request-path statement has the 30 s server timeout.

| Variable | Default | Meaning |
| --- | --- | --- |
| `EXPLORE_HUBS_REFRESH_ENABLED` | `true` | Compute hub discoveries in the background |
| `EXPLORE_HUBS_REFRESH_INTERVAL` | `3600` | Seconds between checks for missing or changed hubs |
| `EXPLORE_HUBS_MAX_AGE` | `604800` | Seconds after which a hub is recomputed even if unchanged |

## Not covered

An artist with a very large discography (Various, 1.5M releases) is still walked live and returns `504` at the
30 s budget instead of running for minutes. It has no meaningful neighbourhood.
