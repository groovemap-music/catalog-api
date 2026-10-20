"""The similar-artist precomputed switch and fusion alpha (gm-catalog-api-2zsq.1)."""

from __future__ import annotations

import pytest

from api.config import DEFAULT_FUSION_ALPHA, ApiConfig, _env_unit_float


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIMILAR_ARTISTS_FUSION_ALPHA", raising=False)


def test_defaults_are_off_and_point_eight() -> None:
    assert DEFAULT_FUSION_ALPHA == 0.8
    fields = ApiConfig.__dataclass_fields__
    assert fields["similar_artists_precomputed_enabled"].default is False
    assert fields["similar_artists_fusion_alpha"].default == 0.8


def test_alpha_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _env_unit_float("SIMILAR_ARTISTS_FUSION_ALPHA", 0.8) == 0.8
    monkeypatch.setenv("SIMILAR_ARTISTS_FUSION_ALPHA", "0.65")
    assert _env_unit_float("SIMILAR_ARTISTS_FUSION_ALPHA", 0.8) == 0.65
    monkeypatch.setenv("SIMILAR_ARTISTS_FUSION_ALPHA", " ")
    assert _env_unit_float("SIMILAR_ARTISTS_FUSION_ALPHA", 0.8) == 0.8


@pytest.mark.parametrize("raw", ["1.5", "-0.1", "high", "nan"])
def test_an_invalid_alpha_is_a_startup_error_not_a_silent_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SIMILAR_ARTISTS_FUSION_ALPHA", raw)
    with pytest.raises(ValueError, match="SIMILAR_ARTISTS_FUSION_ALPHA"):
        _env_unit_float("SIMILAR_ARTISTS_FUSION_ALPHA", 0.8)
