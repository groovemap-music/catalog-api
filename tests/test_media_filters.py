"""Tests for api/queries/media_filters.py."""

import pytest

from api.queries.media_filters import (
    UnknownMediaIdsError,
    media_ids_from_formats,
    resolve_media_filter,
    split_media_ids,
    validate_media_ids,
)


class TestValidateMediaIds:
    def test_none_returns_empty(self) -> None:
        assert validate_media_ids(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert validate_media_ids([]) == []

    def test_known_family_id_passes(self) -> None:
        assert validate_media_ids(["vinyl"]) == ["vinyl"]

    def test_known_medium_id_passes(self) -> None:
        assert validate_media_ids(["vinyl_12"]) == ["vinyl_12"]

    def test_mixed_family_and_medium_ids_pass(self) -> None:
        assert validate_media_ids(["vinyl", "optical_cd"]) == ["vinyl", "optical_cd"]

    def test_unknown_id_raises(self) -> None:
        with pytest.raises(UnknownMediaIdsError) as exc_info:
            validate_media_ids(["not-a-real-id"])
        assert exc_info.value.unknown == ["not-a-real-id"]
        assert "not-a-real-id" in str(exc_info.value)

    def test_multiple_unknown_ids_all_listed_sorted(self) -> None:
        with pytest.raises(UnknownMediaIdsError) as exc_info:
            validate_media_ids(["zzz-bad", "aaa-bad", "vinyl"])
        assert exc_info.value.unknown == ["aaa-bad", "zzz-bad"]


class TestMediaIdsFromFormats:
    def test_none_returns_empty(self) -> None:
        assert media_ids_from_formats(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert media_ids_from_formats([]) == []

    def test_known_format_name_maps_to_family_and_medium(self) -> None:
        result = media_ids_from_formats(["CD"])
        assert "optical" in result
        assert "optical_cd" in result

    def test_unrecognised_format_name_maps_to_nothing(self) -> None:
        assert media_ids_from_formats(["NotARealFormat"]) == []


class TestSplitMediaIds:
    def test_empty_returns_two_empty_lists(self) -> None:
        assert split_media_ids([]) == ([], [])

    def test_splits_families_and_mediums(self) -> None:
        families, mediums = split_media_ids(["vinyl", "optical_cd", "digital"])
        assert families == ["digital", "vinyl"]
        assert mediums == ["optical_cd"]


class TestResolveMediaFilter:
    def test_media_only(self) -> None:
        families, mediums = resolve_media_filter(["vinyl"], None)
        assert families == ["vinyl"]
        assert mediums == []

    def test_formats_only_maps_through_taxonomy(self) -> None:
        families, mediums = resolve_media_filter(None, ["CD"])
        assert families == ["optical"]
        assert mediums == ["optical_cd"]

    def test_media_and_formats_combine(self) -> None:
        families, mediums = resolve_media_filter(["digital"], ["CD"])
        assert families == ["digital", "optical"]
        assert mediums == ["optical_cd"]

    def test_neither_returns_empty(self) -> None:
        assert resolve_media_filter(None, None) == ([], [])

    def test_unknown_media_id_raises_even_with_formats_present(self) -> None:
        with pytest.raises(UnknownMediaIdsError):
            resolve_media_filter(["bogus"], ["CD"])
