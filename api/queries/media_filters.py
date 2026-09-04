"""Shared helpers for the ADR 0007 canonical ``media`` filter.

Gap analysis (and any future consumer) accepts a ``media`` query parameter of
family or medium ids from the canonical taxonomy (``common.media``). The
legacy ``formats`` parameter — raw Discogs format names — survives as a
deprecated alias for one minor version: it is mapped onto the same canonical
ids through :func:`common.media.legacy_format_names_to_media` so both
parameters filter through one code path.
"""

from __future__ import annotations

from common.media import family_ids, legacy_format_names_to_media, medium_ids


class UnknownMediaIdsError(ValueError):
    """Raised when a ``media`` filter includes ids the taxonomy does not know."""

    def __init__(self, unknown: list[str]) -> None:
        self.unknown = unknown
        super().__init__(f"Unknown media id(s): {', '.join(unknown)}")


def _known_media_ids() -> frozenset[str]:
    return frozenset(family_ids()) | frozenset(medium_ids())


def validate_media_ids(media: list[str] | None) -> list[str]:
    """Return ``media`` unchanged when every id is a known family or medium id.

    Args:
        media: Requested family/medium ids, or ``None``.

    Returns:
        ``media`` as given (``[]`` when ``None``).

    Raises:
        UnknownMediaIdsError: If any id is not in the taxonomy.
    """
    if not media:
        return []
    known = _known_media_ids()
    unknown = sorted({m for m in media if m not in known})
    if unknown:
        raise UnknownMediaIdsError(unknown)
    return media


def media_ids_from_formats(formats: list[str] | None) -> list[str]:
    """Map the deprecated ``formats`` (raw Discogs format names) onto canonical ids.

    Args:
        formats: Raw Discogs format names as received from the request, or ``None``.

    Returns:
        The sorted, unique family and medium ids the vocabulary resolves them to.
        A name the vocabulary does not recognise contributes nothing (it lands in
        the mapped block's ``unmapped`` list, which is not surfaced here).
    """
    if not formats:
        return []
    block = legacy_format_names_to_media(formats)
    families = block.get("families") or []
    mediums = sorted({item["medium"] for item in block.get("items", []) if item.get("medium")})
    return sorted(set(families) | set(mediums))


def split_media_ids(media_ids: list[str]) -> tuple[list[str], list[str]]:
    """Split a combined id list into ``(families, mediums)`` for Cypher params."""
    if not media_ids:
        return [], []
    family_set = frozenset(family_ids())
    medium_set = frozenset(medium_ids())
    families = sorted({m for m in media_ids if m in family_set})
    mediums = sorted({m for m in media_ids if m in medium_set})
    return families, mediums


def resolve_media_filter(media: list[str] | None, formats: list[str] | None) -> tuple[list[str], list[str]]:
    """Combine the ``media`` and deprecated ``formats`` filters into ``(families, mediums)``.

    Args:
        media: Requested family/medium ids, validated against the taxonomy.
        formats: Deprecated raw Discogs format names, mapped through the shared
            runtime helper.

    Returns:
        A ``(families, mediums)`` pair ready for the gap queries' Cypher params.

    Raises:
        UnknownMediaIdsError: If ``media`` includes an id the taxonomy does not know.
    """
    validated = validate_media_ids(media)
    combined = sorted(set(validated) | set(media_ids_from_formats(formats)))
    return split_media_ids(combined)
