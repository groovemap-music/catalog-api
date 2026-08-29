"""Compliance metadata exposed through the API documentation."""

from api.api import SOURCE_REPOSITORY, app, source_url


def test_source_url_identifies_exact_revision() -> None:
    revision = "a" * 40

    assert source_url(revision) == f"{SOURCE_REPOSITORY}/tree/{revision}"


def test_source_url_rejects_non_revision_values() -> None:
    assert source_url(None) == SOURCE_REPOSITORY
    assert source_url("main") == SOURCE_REPOSITORY
    assert source_url("A" * 40) == SOURCE_REPOSITORY


def test_openapi_advertises_license_and_corresponding_source() -> None:
    schema = app.openapi()

    assert schema["info"]["license"]["identifier"] == "AGPL-3.0-only"
    assert schema["externalDocs"]["description"] == "Corresponding source for this API revision"
    assert schema["externalDocs"]["url"].startswith(SOURCE_REPOSITORY)
