# CrateFit — the item-in-hand fit profile

`GET /api/fit/release/{release_id}` answers the question a collector asks with a record
in their hands: *given everything I already own, is this one for me?*

The answer is not a number. It is a decomposition into five named components, each with
its own score in `[0, 1]` and its own short evidence list stated in the caller's own
holdings, plus a combined `fit` that is an arithmetic consequence of the five rather than
the thing they explain. A collector who disagrees with the score can see which component
produced it and check that component against their own shelves.

This is **version 0**. Every heuristic below is a stand-in for something the service does
not yet compute, and the page says which. The impressions the endpoint writes are what
will eventually replace the guesses with learned weights.

## The response

```json
{
  "release": {
    "id": "555",
    "gm_id": "0199…",
    "title": "Blue Train",
    "artist": "John Coltrane",
    "year": 1957,
    "media_families": ["grooved"],
    "rarity": {"score": 72.5, "tier": "scarce"}
  },
  "fit": 0.82,
  "components": {
    "affinity":   {"score": 0.55, "evidence": ["shares label Blue Note with 3 releases you hold"],
                   "evidence_items": [{"dimension": "label", "entity": "Blue Note", "kind": "shared", "count": 3}]},
    "novelty":    {"score": 0.25, "evidence": ["John Coltrane is an artist your collection has never held"],
                   "evidence_items": [{"dimension": "artist", "entity": "John Coltrane", "kind": "unheld"}]},
    "bridge":     {"score": 0.0,  "evidence": [], "evidence_items": []},
    "depth":      {"score": 0.6,  "evidence": ["deepens label Blue Note (3 held)"],
                   "evidence_items": [{"dimension": "label", "entity": "Blue Note", "kind": "thread", "count": 3}]},
    "redundancy": {"score": 0.0,  "evidence": [], "evidence_items": []}
  },
  "confidence": "exact",
  "policy_id": "cratefit_v0",
  "fit_version": "cratefit_v0",
  "impression_id": "0199…"
}
```

| Field | Meaning |
| --- | --- |
| `fit` | `affinity + novelty + bridge + depth − redundancy`, clipped to `[0, 1]`. Unweighted |
| `components` | The five dimensions, each `{score, evidence, evidence_items}`, plus `evidence_entries` for a first-party session. Always all five |
| `confidence` | How the candidate was **identified**, not how good the fit is. See below |
| `policy_id` / `fit_version` | The decision procedure that produced the score. The same string |
| `impression_id` | The showing this response was, to report an outcome against. `null` when the release has no native id |
| `release.rarity` | Read from the precomputed `release_rarity` table. Never computed on the request path, and **not an input to any component** |

## The five components

**Affinity — how much of this record you already collect.** Per dimension, the fraction
of the *candidate's* facets the collector holds, combined by weights of 0.35 (artist),
0.25 (label), 0.25 (genre), 0.15 (style) and renormalised over the dimensions the
candidate actually has. A release the catalogue carries no style for is scored on artist,
label, and genre rather than penalised for a gap in the data.

**Novelty — how much of it is new to you.** Every facet the candidate carries is pooled
and the score is the unheld fraction. Pooled rather than weighted, because novelty is a
statement about the record and not about which dimension of taste it lands in.

**Depth — how far it extends something you are already building.** The deepest single
thread the record continues, over its labels and artists, saturating at five held. A
maximum rather than a sum: a record on a label you have ten of does not become deeper for
also being by an artist you have one of.

**Bridge — whether it connects corners of your collection that do not touch.** *A v0
heuristic, and the component to trust least.* See the next section.

**Redundancy — whether you already have it.** Four cases, strongest first, and the
strongest wins outright:

| Case | Score |
| --- | --- |
| You hold this exact release | 1.0 |
| You hold another release of the same master on a media family this one also carries | 1.0 |
| You hold another release of the same master on a different family | 0.6 |
| You hold a record of the same title by the same artist that no master links to this one | 0.3 |

Redundancy is the one component subtracted from the fit, which is why its evidence names
the held version: a collector told their fit is low deserves to be told which record of
theirs said so.

## Evidence, structured

Each component's `evidence` is a short list of sentences, and `evidence_items` is the same
facts one entry per sentence, at the same position — a client that wants to key on the
claim rather than parse the sentence reads exactly what the sentence says, because the
sentence is rendered from the entry rather than composed beside it. Both lists are capped
by the same limit (three), so a component's evidence is never deeper on one side than the
other.

An entry always carries:

| Field | Meaning |
| --- | --- |
| `dimension` | The facet the claim is about: `artist`, `label`, `genre`, `style`, `release`, or a component-specific one like `bridge` |
| `entity` | The name or id of the thing the claim is about |
| `kind` | The shape of the claim — see below |

and, when the claim has one:

| Field | Meaning |
| --- | --- |
| `count` | The collector's held count for the facet (affinity's and depth's evidence) |
| `release_id` | The matched release id (redundancy's evidence, when a specific held release is named) |
| `detail` | A little extra text a few claim kinds need to finish their sentence — a shared media family, the artist name a title match was found under |

`kind` names the assertion, not the dimension: `shared` (affinity — the candidate and the
collection share this facet), `unheld` (novelty — the candidate carries this facet and the
collection does not), `thread` (depth — this facet is a thread the candidate deepens),
`bridge` and `heuristic` (bridge's two evidence lines: the regions joined, and the caveat
that the regions are a v0 genre heuristic), and `duplicate` / `duplicate_title`
(redundancy's four cases — the last, with no master link between the two releases, is its
own kind because its sentence names an artist rather than a shared media family).

An evidence item never claims more than its own sentence does: `evidence` and
`evidence_items` are two readings of one fact, not two facts that happen to agree.

## Evidence entries, uncapped and with ids

`evidence_entries` is every entry behind a component's score, with no cap, in the same
order as `evidence_items`. Only a first-party session receives it; an app token never does
(see [Delegated access](#delegated-access)). Below the cap, entry `i` is item `i` with keys added, so the
two lists never state different facts. `evidence` and `evidence_items` are unchanged by
it, byte for byte, and `tests/fixtures/fit_legacy_evidence.json` pins them.

It exists for the Strata atlas (design ADR 0016, concept C′). The atlas draws every
entry it can place and lists the rest beside the map. The cap of three suits a person
reading sentences, but it can hide a drawable unheld style behind three unheld artists
and labels, which happens in 5.9% of scored novelty cases.

```json
"novelty": {
  "score": 0.6,
  "evidence": ["Xavier is an artist your collection has never held", "…", "…"],
  "evidence_items": [{"dimension": "artist", "entity": "Xavier", "kind": "unheld"}, "…", "…"],
  "evidence_entries": [
    {"dimension": "artist", "entity": "Xavier",   "kind": "unheld", "entity_type": "artist", "entity_id": "1001",  "drawable": false},
    {"dimension": "artist", "entity": "Yusef",    "kind": "unheld", "entity_type": "artist", "entity_id": "1002",  "drawable": false},
    {"dimension": "artist", "entity": "Zoot",     "kind": "unheld", "entity_type": "artist", "entity_id": "1003",  "drawable": false},
    {"dimension": "label",  "entity": "Impulse!", "kind": "unheld", "entity_type": "label",  "entity_id": "2001",  "drawable": false},
    {"dimension": "style",  "entity": "Modal",    "kind": "unheld", "entity_type": "style",  "entity_id": "Modal", "drawable": true}
  ]
},
"bridge": {
  "evidence_entries": [
    {"dimension": "genre", "entity": "Electronic and Jazz", "kind": "bridge", "count": 2,
     "genres": ["Electronic", "Jazz"], "drawable": true},
    {"dimension": "bridge", "entity": "v0 heuristic", "kind": "heuristic", "drawable": false}
  ]
}
```

An entry carries every `evidence_items` field, and also:

| Field | Meaning |
| --- | --- |
| `entity_type` | What `entity_id` names: `artist`, `label`, `genre`, `style`, or `release`. Read this rather than inferring the type from the id, because a Discogs id and a genre name are both strings. Present exactly when `entity_id` is |
| `entity_id` | The graph id of `entity`, in the same form `/api/explore` and `/api/explore/strata` return, so a client can join on it. An artist, label, or release is its graph node id as a string (the Discogs id). A genre or style is its name, because the graph keys them by name. Absent on bridge entries, which name several genres (see `genres`), and on the heuristic caveat |
| `genres` | Bridge only: the bridged regions as a list, in the order the sentence names them. Read this, not the sentence, because a genre name can contain commas (`Folk, World, & Country`) |
| `master_id` | Redundancy `duplicate` only: the master the held release hangs under, when the candidate has one |
| `drawable` | Always present. Whether the atlas can place the claim |

`drawable` is decided per component. The atlas has no plane for artists or labels, so
their entries are always listed beside the map rather than drawn on it.

| Component | Drawable (`true`) | Listed beside the map (`false`) |
| --- | --- | --- |
| Affinity | A shared genre or style, as its node | A shared artist or label |
| Novelty | An unheld genre or style, as its node | An unheld artist or label |
| Bridge | The bridge entry, as the edge between the genre hulls in `genres` | The heuristic caveat |
| Depth | Nothing | Every entry. Depth threads are artists and labels only |
| Redundancy | A `duplicate` when the candidate has a master, as the held release in that master's slot (`master_id`) | A `duplicate` of a masterless release, and every `duplicate_title`. Neither has a master to sit under |

A title-only duplicate's `entity_id` is the lowest id among the held releases with that
artist and title.

For up to ten minutes after the deploy that adds these fields, a collection folded before
it is still cached, and a title-only duplicate then lacks its `entity_id` and
`entity_type`. The session profile is cached under a key of its own, so it never comes
from a body cached before `evidence_entries` existed.

## What is a v0 heuristic, and why bridge is the loudest one

The real bridge question is whether a record joins two *communities* of a collector's
graph. Nobody has computed those communities. So version 0 stands the collection's genres
in for them:

- a genre the collector holds is a **region**;
- the candidate **touches** a region when it shares that genre, or when one of its styles
  appears among the styles of the collector's releases in that region;
- two regions count as **separate** only when the collector's releases in them share no
  artist and no label;
- the score is (separate regions touched − 1), capped at 1.0.

The separate-region set is built greedily in sorted order rather than by finding the
largest mutually-separate subset. The greedy pass is deterministic and linear; the exact
version is an independent-set search, and v0 does not have the evidence to justify the
difference. Genre is coarse and commercially assigned, so a high bridge score is a hint
worth showing a collector and not a claim about their collection's structure. The
component says so in its own evidence.

The other numbers are softer than they look too. The affinity weights are not learned —
they mirror the artist-similarity weights already in `recommend_queries` — the depth
saturation point of five held is a judgement, and the four redundancy scores are a
ranking with plausible gaps rather than measured quantities. All of them live in one dict,
`api.fit.FIT_CONSTANTS`, so the whole of v0's judgement is readable in one place.

## Identity confidence is not fit confidence

`confidence` answers *which record is this?*, not *how good a fit is it?* — and the two
are reported side by side rather than multiplied, because multiplying them would make a
confident bad fit indistinguishable from an unsure good one.

| Value | Meaning |
| --- | --- |
| `exact` | The candidate resolved to a release. This pressing |
| `master` | Only the master resolved. The same record, but not necessarily this pressing |

## Limits of version 0

- **Discogs release id only.** The path parameter is a Discogs release id. Barcode and
  catalogue-number identification — the thing a collector in a shop actually has to hand —
  arrives with the catalog-identifiers programme, and the `master` confidence value exists
  so the response shape does not have to change when it does.
- **No barcode, no catalogue number, no photograph.** A record whose Discogs id the caller
  does not know cannot be scored yet.
- **`date_added` is the Discogs date-added**, not the date the collector acquired the
  record. Nothing in v0 reads it, and anything that later does should say which date it
  means.
- **Rarity is shown, not scored.** `release.rarity` is read from the precomputed
  `release_rarity` table beside the fit answer. It is deliberately not an input: rarity is a fact about the
  record, fit is a fact about the record *and this collector*, and folding one into the
  other would make a common record the collector obviously wants look like a worse buy
  than a rare one they do not.
- **No learned weights.** Every constant is a judgement. That is what `fit_version` is
  for.
- **A fit profile is a statement about a collection**, so it is cached per `(user,
  release)` for ten minutes under a key the collection sync's cache invalidation already
  sweeps.

## Delegated access

The endpoint takes a first-party session **or** an app token carrying `fit:read`.

`fit:read` is deliberately not folded into `collection:read`. The two authorise very
different things: a kiosk scoring a record in a shop needs to *use* the collection to
answer, and has no business listing what is in it.

For the same reason an app token never receives `evidence_entries`, whatever scopes it
carries. Uncapped, the entries name held releases and every held facet the candidate
touches, which is close to a listing. An app token gets the capped profile, byte for byte
as it was before the field existed, and it is cached under a separate key from the session
profile so the cache cannot cross the two.

## The impression

Every request served — cache hit or miss — writes one impression, so an outcome reported
later can be joined back to the exact showing that produced it. The cached body never
carries an `impression_id`: an impression records a list having been *shown*, and the
request that filled the cache is not the request that shows it to the next caller.

| Field | Value |
| --- | --- |
| `surface` | `fit` |
| `policy_id` | `cratefit_v0` |
| `position` | `1` — CrateFit ranks nothing; it answers about the one record the caller named |
| `score` | The combined `fit` |
| `propensity` | `1.0` — the policy is deterministic and samples nothing |
| `item_id` | The release's native id |

A release the alias table does not carry has no native id and therefore no impression. The
profile is still returned, `impression_id` is `null`, and the gap is counted — the same
way the recommendation surfaces count theirs.

The surface vocabulary is vendored from `groovemap-runtime` at a pinned revision and now
carries its own `fit` surface, with `fit.shown`, `fit.opened`, `fit.saved`,
`fit.dismissed`, and `fit.hidden` event types alongside it. `api.activity.SURFACE_FIT` is
the literal `"fit"`, and `policy_id` is still what tells a fit row apart from an explore or
similar-artist row within that surface. `fit.opened`, `fit.saved`, `fit.dismissed`, and
`fit.hidden` are reportable by a client through `POST /api/activity/events`, the same way
the equivalent `recommendation.*` outcomes are.
