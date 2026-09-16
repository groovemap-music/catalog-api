"""CrateFit v0 — the pure scoring behind the item-in-hand fit profile.

A collector standing in a shop with a record in their hand is not asking for a number.
They are asking five separate questions at once — *is this my kind of thing, is any of it
new to me, does it deepen something I am already building, does it connect two corners of
my collection that do not touch, and do I already own it?* — and a single score answers
none of them. So the decomposition is the product: every component carries its own score
in ``[0, 1]`` and its own evidence, stated in the collector's own holdings, and the
combined fit is an arithmetic consequence of the five rather than the thing the five
explain.

Everything here is pure. The inputs are the folded collection from
:func:`api.queries.fit_queries.get_collection_ids` and the candidate context from
:func:`api.queries.fit_queries.get_release_context`; there is no driver, no pool, no
clock, and no randomness, so the same two inputs always produce the same profile and the
whole module is testable on hand-built dictionaries.

Three things are stated rather than implied:

* **The weights are not learned.** They are the ones in :data:`FIT_CONSTANTS`, chosen to
  mirror the artist-similarity weights already in ``recommend_queries``. Version 0 has no
  training signal; the impressions this surface writes are what will eventually produce
  one, which is exactly why every row carries :data:`FIT_VERSION`.
* **Bridge is a heuristic and is labelled as one.** See :func:`score_bridge`. It is the
  only component with no analogue anywhere else in the service, and its notion of a
  "region" is a stand-in for a community structure nobody has computed yet.
* **Identity confidence is not fit confidence.** :func:`identity_confidence` reports how
  sure the service is that it is scoring *the record in the collector's hand*, which is a
  different question from how good a fit that record is, and the two are reported side by
  side rather than multiplied together.
"""

from __future__ import annotations

from typing import Any, Final

# The one import from the query layer, and it is a pure key function rather than a read:
# the same rule has to build the held-title index and look a candidate up in it, and two
# copies of a case-folding rule are two copies that drift.
from api.queries.fit_queries import held_title_key


__all__ = [
    "FIT_CONSTANTS",
    "FIT_VERSION",
    "compute_fit",
    "identity_confidence",
    "score_affinity",
    "score_bridge",
    "score_depth",
    "score_novelty",
    "score_redundancy",
]

# The decision procedure, named and versioned. A change to any constant or any formula in
# this module is a new version string, never a redefinition of this one: the policy id on
# a stored impression is historical data, and an offline evaluation that cannot tell which
# procedure produced a row cannot learn anything from it.
FIT_VERSION: Final = "cratefit_v0"

# Every tunable in one dict, so the whole of version 0's judgement is readable in one
# place and a version 1 is a diff against it rather than an archaeology exercise.
FIT_CONSTANTS: Final[dict[str, Any]] = {
    # Mirrors the shape of `recommend_queries._WEIGHTS`: four dimensions, 0.35 / 0.25 /
    # 0.25 / 0.15, with the artist rather than the genre carrying the most weight because
    # a collector's artists are a sharper statement of taste than their genres are.
    "affinity_weights": {"artist": 0.35, "label": 0.25, "genre": 0.25, "style": 0.15},
    # Five held releases on a label or by an artist is "you are building this", so depth
    # saturates there rather than growing without bound into a collection's biggest label.
    "depth_saturation": 5,
    "bridge_score_cap": 1.0,
    "redundancy_held_release": 1.0,
    "redundancy_same_master_same_family": 1.0,
    "redundancy_same_master_other_family": 0.6,
    "redundancy_same_artist_and_title": 0.3,
    # Evidence is read by a person holding a record, so each component cites at most a few
    # facts rather than everything it could.
    "evidence_limit": 3,
    "score_precision": 4,
}

_PRECISION: Final[int] = int(FIT_CONSTANTS["score_precision"])
_EVIDENCE_LIMIT: Final[int] = int(FIT_CONSTANTS["evidence_limit"])

# The singular noun each dimension is named by in an evidence string.
_DIMENSION_NOUN: Final[dict[str, str]] = {"artist": "artist", "label": "label", "genre": "genre", "style": "style"}

# Where each dimension's held ids and per-facet counts live in the folded collection.
_HELD_KEYS: Final[dict[str, str]] = {"artist": "artist_ids", "label": "label_ids", "genre": "genres", "style": "styles"}
_COUNT_KEYS: Final[dict[str, str]] = {"artist": "artist_counts", "label": "label_counts", "genre": "genre_counts", "style": "style_counts"}


def _article(noun: str) -> str:
    """The indefinite article an evidence string needs before ``noun``."""
    return "an" if noun[0] in "aeiou" else "a"


def _entry(
    dimension: str,
    entity: str,
    kind: str,
    *,
    count: int | None = None,
    release_id: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """One structured evidence fact: what a component asserts, before it is worded.

    ``dimension`` names the facet the claim is about (``artist``, ``label``, ``genre``,
    ``style``, ``release``, ...), ``entity`` the name or id of the thing the claim is
    about, and ``kind`` the shape of the claim itself (``shared``, ``unheld``, ``thread``,
    ``duplicate``, ``bridge``, or a component-specific kind such as ``duplicate_title`` or
    ``heuristic``). ``count`` and ``release_id`` hold the held count or the matched release
    id where the claim has one; ``detail`` is the small amount of extra freeform text a few
    claim kinds need to complete their sentence (a shared media family, a matched artist
    name) and is never the only fact a sentence states.
    """
    entry: dict[str, Any] = {"dimension": dimension, "entity": entity, "kind": kind}
    if count is not None:
        entry["count"] = count
    if release_id is not None:
        entry["release_id"] = release_id
    if detail is not None:
        entry["detail"] = detail
    return entry


def _render_entry(entry: dict[str, Any]) -> str:
    """The one place an evidence sentence is written, so a sentence and its entry cannot drift.

    Every ``evidence`` string a component returns is this function applied to the entry at
    the same position in ``evidence_items`` — never a string composed separately from the
    same facts — which is what makes the two lists a single source of truth read two ways.
    """
    kind = entry["kind"]
    dimension = entry.get("dimension", "")
    entity = entry.get("entity", "")
    count = entry.get("count")
    detail = entry.get("detail")

    if kind == "shared":
        return f"shares {dimension} {entity} with {count} release{'' if count == 1 else 's'} you hold"
    if kind == "unheld":
        return f"{entity} is {_article(dimension)} {dimension} your collection has never held"
    if kind == "thread":
        return f"deepens {dimension} {entity} ({count} held)"
    if kind == "bridge":
        return f"bridges {entity}, which share no artist or label in your collection"
    if kind == "heuristic":
        return "region boundaries are the v0 genre heuristic, not a computed community"
    if kind == "duplicate":
        if detail is None:
            return "you already hold this exact release"
        return f"you hold {entity}, the same record on {detail}"
    if kind == "duplicate_title":
        return f"you hold {entity} by {detail}, which no master links to this pressing"
    raise ValueError(f"unrenderable evidence kind: {kind!r}")


def _component(score: float, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape one component: a score clipped to ``[0, 1]``, and evidence built from entries.

    ``evidence`` and ``evidence_items`` are two views of the same facts, capped by the
    same :data:`FIT_CONSTANTS` evidence limit: every entry is rendered into its sentence by
    :func:`_render_entry`, so the prose a collector reads and the structured claim a
    consumer can key on can never say two different things.
    """
    capped = entries[:_EVIDENCE_LIMIT]
    return {
        "score": round(min(1.0, max(0.0, score)), _PRECISION),
        "evidence": [_render_entry(entry) for entry in capped],
        "evidence_items": capped,
    }


def _candidate_facets(context: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Return the candidate's ``(id, display name)`` pairs per dimension.

    Artists and labels are keyed by id and shown by name — two labels genuinely share a
    name often enough that matching on the name would report an overlap that is not one.
    Genres and styles are name-keyed in the graph, so for them the id *is* the name.
    """
    facets: dict[str, list[tuple[str, str]]] = {}
    for dimension, entries in (("artist", context.get("artists")), ("label", context.get("labels"))):
        pairs: list[tuple[str, str]] = []
        for entry in entries or []:
            identifier = entry.get("id")
            if identifier:
                pairs.append((str(identifier), str(entry.get("name") or identifier)))
        facets[dimension] = pairs
    for dimension, key in (("genre", "genres"), ("style", "styles")):
        facets[dimension] = [(str(value), str(value)) for value in context.get(key) or [] if value]
    return facets


def _held(collection: dict[str, Any], dimension: str) -> set[str]:
    """The ids the collector holds in one dimension."""
    return {str(value) for value in collection.get(_HELD_KEYS[dimension]) or []}


def _counts(collection: dict[str, Any], dimension: str) -> dict[str, int]:
    """How many held releases carry each facet of one dimension."""
    raw = collection.get(_COUNT_KEYS[dimension]) or {}
    return {str(key): int(value) for key, value in raw.items()}


def score_affinity(collection: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """How much of the candidate the collector already collects.

    Per dimension, the overlap is the fraction of the *candidate's* facets the collector
    holds, so a record on one label the collector already buys scores the label dimension
    at one rather than at one-over-the-size-of-their-collection. The dimensions are then
    combined by :data:`FIT_CONSTANTS`'s weights, renormalised over the dimensions the
    candidate actually has: a release the graph carries no style for is scored on artist,
    label, and genre rather than penalised for a gap in the catalogue data.
    """
    weights: dict[str, float] = FIT_CONSTANTS["affinity_weights"]
    facets = _candidate_facets(context)

    weighted = 0.0
    weight_total = 0.0
    shared: list[tuple[int, str, str]] = []
    for dimension, weight in weights.items():
        pairs = facets[dimension]
        if not pairs:
            continue
        held = _held(collection, dimension)
        counts = _counts(collection, dimension)
        matches = [(identifier, name) for identifier, name in pairs if identifier in held]
        weighted += weight * (len(matches) / len(pairs))
        weight_total += weight
        shared.extend((counts.get(identifier, 0), _DIMENSION_NOUN[dimension], name) for identifier, name in matches)

    score = weighted / weight_total if weight_total else 0.0
    # Loudest first, then alphabetically, so the same collection always cites the same
    # facts in the same order.
    shared.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
    entries = [_entry(noun, name, "shared", count=count) for count, noun, name in shared if count]
    return _component(score, entries)


def score_novelty(collection: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """How much of the candidate is new to the collection.

    Every facet the candidate carries — each artist, label, genre, and style — is pooled
    and the score is the fraction of that pool the collector does not already hold. Pooled
    rather than weighted because novelty is a statement about the *record*, not about
    which dimension of taste it lands in: a record that is new in every way scores one
    however its facets are distributed.
    """
    facets = _candidate_facets(context)
    total = 0
    new: list[tuple[str, str]] = []
    for dimension in FIT_CONSTANTS["affinity_weights"]:
        pairs = facets[dimension]
        if not pairs:
            continue
        held = _held(collection, dimension)
        total += len(pairs)
        new.extend((_DIMENSION_NOUN[dimension], name) for identifier, name in pairs if identifier not in held)

    score = len(new) / total if total else 0.0
    new.sort()
    return _component(score, [_entry(noun, name, "unheld") for noun, name in new])


def score_depth(collection: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """How far the candidate extends something the collector is already building.

    The maximum over the candidate's labels and artists of the collector's holding in
    each, saturating at :data:`FIT_CONSTANTS`'s ``depth_saturation``. A maximum rather
    than a sum because depth is about the one thread the record continues, and a record on
    a label the collector has ten of does not become deeper for also being by an artist
    they have one of.
    """
    saturation = int(FIT_CONSTANTS["depth_saturation"])
    facets = _candidate_facets(context)

    score = 0.0
    deepened: list[tuple[int, str, str]] = []
    for dimension in ("label", "artist"):
        counts = _counts(collection, dimension)
        for identifier, name in facets[dimension]:
            held = counts.get(identifier, 0)
            if held <= 0:
                continue
            score = max(score, min(1.0, held / saturation))
            deepened.append((held, _DIMENSION_NOUN[dimension], name))

    deepened.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
    return _component(score, [_entry(noun, name, "thread", count=count) for count, noun, name in deepened])


def score_bridge(collection: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Whether the candidate connects corners of the collection that do not touch.

    **Version 0 heuristic — read this before trusting the number.** The real question is
    whether a record joins two communities of a collector's graph, and nobody has computed
    those communities. So version 0 stands in the collection's *genres* for them: a genre
    is a region, the candidate touches a region when it shares that genre or when one of
    its styles appears among the styles of the collector's releases in that region, and
    two regions count as separate only when the collector's releases in them share no
    artist and no label. The score is the number of mutually separate regions the
    candidate touches, minus one, capped at one — so touching one region scores nothing
    and touching two scores the cap.

    The region set is built greedily in sorted order rather than by finding the largest
    mutually-separate subset, which is deliberate: the greedy pass is deterministic and
    linear, the exact version is an independent-set search, and version 0 does not have
    the evidence to justify the difference. Genre is a coarse and commercially-assigned
    proxy for a community, so a high bridge score here is a hint worth showing a collector
    and not a claim about their collection's structure.
    """
    regions = [str(value) for value in collection.get("genres") or []]
    genre_styles: dict[str, Any] = collection.get("genre_styles") or {}
    genre_artists: dict[str, Any] = collection.get("genre_artists") or {}
    genre_labels: dict[str, Any] = collection.get("genre_labels") or {}

    candidate_genres = {str(value) for value in context.get("genres") or []}
    candidate_styles = {str(value) for value in context.get("styles") or []}

    touched = [
        region
        for region in sorted(regions)
        if region in candidate_genres or (candidate_styles & {str(value) for value in genre_styles.get(region) or []})
    ]

    picked: list[str] = []
    picked_neighbourhoods: list[set[str]] = []
    for region in touched:
        neighbourhood = {str(value) for value in genre_artists.get(region) or []} | {str(value) for value in genre_labels.get(region) or []}
        if any(neighbourhood & existing for existing in picked_neighbourhoods):
            continue
        picked.append(region)
        picked_neighbourhoods.append(neighbourhood)

    score = min(float(FIT_CONSTANTS["bridge_score_cap"]), float(max(0, len(picked) - 1)))
    entries: list[dict[str, Any]] = []
    if score > 0:
        named = ", ".join(picked[:-1]) + f" and {picked[-1]}"
        entries.append(_entry("genre", named, "bridge", count=len(picked)))
        entries.append(_entry("bridge", "v0 heuristic", "heuristic"))
    return _component(score, entries)


def _sibling_media(sibling: dict[str, Any]) -> set[str]:
    """The media families one sibling release was issued on."""
    return {str(value) for value in sibling.get("media_families") or [] if value}


def score_redundancy(collection: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Whether the collector already has this record, in this form or another.

    Four cases, strongest first, and the strongest match wins outright:

    * the collector holds *this very release*;
    * they hold another release of the same master issued on a media family this one also
      carries — the same record in the same form;
    * they hold another release of the same master on a different family — the same record
      as a different object, which many collectors deliberately want;
    * they hold a record of the same title by the same artist with no master linking the
      two, which is the graph's way of saying "probably the same record" without saying so.

    Redundancy is the one component subtracted from the fit rather than added to it, which
    is why it is scored on the held version and its evidence names that version: a
    collector told their fit is low deserves to be told which record of theirs said so.
    """
    held_releases = {str(value) for value in collection.get("release_ids") or []}
    candidate_id = str(context.get("id") or "")

    if candidate_id and candidate_id in held_releases:
        entry = _entry("release", candidate_id, "duplicate", release_id=candidate_id)
        return _component(float(FIT_CONSTANTS["redundancy_held_release"]), [entry])

    candidate_families = {str(value) for value in context.get("media_families") or [] if value}
    held_siblings = [sibling for sibling in context.get("siblings") or [] if str(sibling.get("id") or "") in held_releases]

    same_family = [sibling for sibling in held_siblings if _sibling_media(sibling) & candidate_families]
    if same_family:
        sibling = min(same_family, key=lambda entry: str(entry.get("id")))
        shared = ", ".join(sorted(_sibling_media(sibling) & candidate_families))
        sibling_id = str(sibling.get("id"))
        entry = _entry("release", str(sibling.get("title") or sibling.get("id")), "duplicate", release_id=sibling_id, detail=shared)
        return _component(float(FIT_CONSTANTS["redundancy_same_master_same_family"]), [entry])

    if held_siblings:
        sibling = min(held_siblings, key=lambda entry: str(entry.get("id")))
        families = ", ".join(sorted(_sibling_media(sibling))) or "another format"
        sibling_id = str(sibling.get("id"))
        entry = _entry("release", str(sibling.get("title") or sibling.get("id")), "duplicate", release_id=sibling_id, detail=families)
        return _component(float(FIT_CONSTANTS["redundancy_same_master_other_family"]), [entry])

    held_titles: dict[str, Any] = collection.get("held_titles") or {}
    title = str(context.get("title") or "")
    if title:
        for identifier, name in _candidate_facets(context)["artist"]:
            held_title = held_titles.get(held_title_key(identifier, title))
            if held_title:
                entry = _entry("release", str(held_title), "duplicate_title", detail=name)
                return _component(float(FIT_CONSTANTS["redundancy_same_artist_and_title"]), [entry])

    return _component(0.0, [])


def identity_confidence(context: dict[str, Any]) -> str:
    """How sure the service is that it scored the record in the collector's hand.

    ``"exact"`` when the candidate resolved to a release, ``"master"`` when only the
    master did — the same record, but not this pressing, so the components that turn on a
    pressing (media family, label, year) are answering about an edition the collector may
    not be holding. Version 0's endpoint takes a Discogs release id, so it answers
    ``"exact"`` today; the master case is what barcode and catalogue-number identification
    will resolve to when the catalog-identifiers programme lands, and it is stated here so
    the response field does not have to change shape when it does.

    Reported beside the fit, never folded into it: how sure we are *which record this is*
    is not the same question as how well it fits, and multiplying the two would make a
    confident bad fit indistinguishable from an unsure good one.
    """
    if context.get("id"):
        return "exact"
    return "master"


def compute_fit(collection: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Score one candidate against one collection and return the whole decomposition.

    ``fit = affinity + novelty + bridge + depth - redundancy``, clipped to ``[0, 1]``.
    Unweighted, because version 0 has no evidence for a weighting and an invented one
    would be indistinguishable from a learned one in the stored impressions. The clip is
    what makes the sum of four additive components and one subtractive one a score; the
    components are returned undamaged beside it, and they are the part a collector reads.
    """
    components = {
        "affinity": score_affinity(collection, context),
        "novelty": score_novelty(collection, context),
        "bridge": score_bridge(collection, context),
        "depth": score_depth(collection, context),
        "redundancy": score_redundancy(collection, context),
    }
    raw = (
        components["affinity"]["score"]
        + components["novelty"]["score"]
        + components["bridge"]["score"]
        + components["depth"]["score"]
        - components["redundancy"]["score"]
    )
    return {
        "fit": round(min(1.0, max(0.0, raw)), _PRECISION),
        "components": components,
        "confidence": identity_confidence(context),
        "fit_version": FIT_VERSION,
    }
