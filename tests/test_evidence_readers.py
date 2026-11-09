"""Authority boundaries and field-level limits of the offline evidence projections."""

from __future__ import annotations

import pytest

from api.evidence_readers import (
    CatalogFact,
    IdentityLedger,
    OwnerObservation,
    read_admin_identity_evidence,
    read_owner_evidence,
    read_public_evidence,
)


def test_public_projection_isolation_and_bounded_output() -> None:
    records: list[CatalogFact | OwnerObservation | IdentityLedger] = [
        CatalogFact(
            "release_details",
            "record-42",
            {
                "id": "42",
                "name": "Original pressing",
                "year": 1977,
                "formats": ["LP"],
                "is_admin": True,
                "owner_private_note": "must not leak",
            },
            subject_ref="native-release-42",
        ),
        CatalogFact(
            "release_details",
            "record-99",
            {"id": "99", "name": "Another record", "year": 1991},
        ),
        OwnerObservation("user-a", {"id": "o1", "kind": "matrix", "value": "deadwax A", "source": "user"}),
        IdentityLedger({"work": {"gm_id": "secret", "providers": [{"provider": "discogs", "external_id": "x"}]}}),
    ]

    page = read_public_evidence(records, limit=2)

    assert page.truncated is True
    assert page.limit == 2
    assert [item["value"] for item in page.items] == ["42", "Original pressing"]
    assert all(item["authority"] == "public_catalog" for item in page.items)
    first, second = page.items
    assert first["source_ref"] == "record-42"
    assert first["subject_ref"] == "native-release-42"
    assert first["field_ref"] == "release_details.id"
    assert first["source_span"] is None
    assert first["time"] == {"value": None, "meaning": "unknown; source query returns no fact timestamp"}
    assert first["method"] == "existing Neo4j release-details query projection"
    assert first["review_state"] == "unknown"
    assert first["review_confidence"] is None
    assert second["source_ref"] == "record-42"
    assert all("must not leak" not in repr(item) and "secret" not in repr(item) for item in page.items)


def test_public_projection_keeps_only_existing_credit_fields() -> None:
    page = read_public_evidence(
        [
            CatalogFact(
                "release_credit",
                "release-context-42",
                {
                    "name": "A. Engineer",
                    "role": "mastered by",
                    "category": "engineering",
                    "artist_id": "person-7",
                    "artist_name": "A. Engineer",
                    "owner_id": "private",
                },
            )
        ]
    )
    assert len(page.items) == 5
    assert {item["field_ref"] for item in page.items} == {
        "release_credit.name",
        "release_credit.role",
        "release_credit.category",
        "release_credit.artist_id",
        "release_credit.artist_name",
    }
    assert all("underlying provider/source attribution unavailable" in item["source"] for item in page.items)
    assert all(item["time"]["value"] is None for item in page.items)
    assert all("private" not in repr(item) for item in page.items)


def test_owner_reader_uses_trusted_owner_context_and_never_includes_other_planes() -> None:
    records: list[CatalogFact | OwnerObservation | IdentityLedger] = [
        CatalogFact("release_details", "release:1", {"id": "1", "name": "Public title"}),
        OwnerObservation(
            "owner-a",
            {
                "id": "obs-a",
                "owned_copy_id": "copy-a",
                "kind": "matrix",
                "value": "A1",
                "source": "user",
                "confidence": 0.7,
                "observed_at": "2026-04-01T10:00:00Z",
                "created_at": "2026-04-02T12:00:00Z",
                # A payload field cannot override the owner attached by server-side context.
                "user_id": "owner-b",
                "review_confidence": 0.99,
            },
        ),
        OwnerObservation(
            "owner-b",
            {"id": "obs-b", "owned_copy_id": "copy-b", "kind": "grade", "value": "VG+", "source": "user", "confidence": 0.95},
        ),
        IdentityLedger({"editions": [{"gm_id": "admin-only", "evidence": [{"provider": "discogs", "external_id": "x"}]}]}),
    ]

    page = read_owner_evidence(records, trusted_owner_id="owner-a")

    assert {item["authority"] for item in page.items} == {"public_catalog", "owner_copy"}
    observations = [item for item in page.items if item["authority"] == "owner_copy"]
    assert {item["source_ref"] for item in observations} == {"obs-a"}
    assert {item["subject_ref"] for item in observations} == {"copy-a"}
    assert all(item["owner_asserted_confidence"] == 0.7 for item in observations)
    assert all(item["review_confidence"] is None and item["review_state"] == "unknown" for item in observations)
    observed = next(item for item in observations if item["field_ref"] == "observation.value")
    stored = next(item for item in observations if item["field_ref"] == "observation.created_at")
    assert observed["time"] == {
        "value": "2026-04-01T10:00:00Z",
        "meaning": "owner/delegate-asserted time the evidence applied; null if not recorded",
    }
    assert stored["time"] == {"value": "2026-04-02T12:00:00Z", "meaning": "row creation time; distinct from observed_at"}
    assert "owner-b" not in repr(page.items)
    assert "admin-only" not in repr(page.items)


def test_owner_reader_does_not_infer_source_span_or_time_when_missing() -> None:
    page = read_owner_evidence(
        [OwnerObservation("owner-a", {"id": "obs-unknown", "kind": "matrix", "value": "etched text", "source": "user"})],
        trusted_owner_id="owner-a",
    )
    item = next(item for item in page.items if item["field_ref"] == "observation.value")
    assert item["source_span"] is None
    assert item["time"]["value"] is None
    assert "null if not recorded" in item["time"]["meaning"]
    assert item["method"] == "stored observation row projection"


def test_admin_reader_requires_server_verified_admin_payload() -> None:
    with pytest.raises(PermissionError, match="server-verified admin"):
        read_admin_identity_evidence([], require_admin_payload={"type": "access", "sub": "user-a"})
    with pytest.raises(PermissionError, match="server-verified admin"):
        read_admin_identity_evidence([], require_admin_payload={"type": "admin"})


def test_admin_reader_projects_only_allowlisted_aliases_and_event_references() -> None:
    records: list[CatalogFact | OwnerObservation | IdentityLedger] = [
        CatalogFact("release_details", "release:1", {"id": "1", "name": "Public title"}),
        OwnerObservation("owner-a", {"id": "obs-a", "kind": "matrix", "value": "private"}),
        IdentityLedger(
            {
                "work": {
                    "gm_id": "work-1",
                    "providers": [
                        {
                            "provider": "discogs",
                            "external_id": "master-1",
                            "valid_from": "2020-01-01T00:00:00Z",
                            "valid_to": None,
                            "owner_id": "should not leak",
                        }
                    ],
                    "events": [
                        {
                            "at": "2021-01-01T00:00:00Z",
                            "type": "link_changed",
                            "provider": "musicbrainz",
                            "external_id": "mb-1",
                            "source_ref": "fullexport:row-7",
                            "decision_ref": "decision:8",
                            "dated_from": "musicbrainz_fullexport",
                            "discogs_id_before": None,
                            "discogs_id_after": "3",
                            "private_user_fact": "do not echo",
                        }
                    ],
                },
                "editions": [
                    {
                        "gm_id": "release-item-1",
                        "evidence": [
                            {
                                "provider": "discogs",
                                "external_id": "release-1",
                                "source": "discogs",
                                "held_by": "native-1",
                                "valid_from": "2022-01-01T00:00:00Z",
                                "valid_to": None,
                                "opened_by": "mint",
                                "closed_by": None,
                                "owner_observation": "private",
                            }
                        ],
                        "events": [
                            {
                                "at": "2022-01-01T00:00:00Z",
                                "type": "reattached",
                                "decision_ref": "audit:9",
                                "dated_from": "groovemap",
                            }
                        ],
                    }
                ],
                "user_observations": ["never echo unknown payload keys"],
            }
        ),
    ]

    page = read_admin_identity_evidence(records, require_admin_payload={"type": "admin", "sub": "admin-1"})

    assert page.truncated is False
    assert len(page.items) == 4
    assert all(item["authority"] == "admin_identity" for item in page.items)
    alias = next(item for item in page.items if item["field_ref"].endswith("edition.provider_alias"))
    assert alias["source"] == "discogs"
    assert alias["source_ref"] == "discogs:release-1"
    assert alias["value"] == {
        "provider": "discogs",
        "external_id": "release-1",
        "source": "discogs",
        "held_by": "native-1",
        "valid_from": "2022-01-01T00:00:00Z",
        "valid_to": None,
        "opened_by": "mint",
        "closed_by": None,
    }
    event = next(item for item in page.items if item["source_ref"] == "fullexport:row-7")
    assert event["source"] == "musicbrainz_fullexport"
    assert event["time"]["meaning"] == "ledger event date attributed to musicbrainz_fullexport"
    assert event["value"]["decision_ref"] == "decision:8"
    assert event["value"]["discogs_id_before"] is None
    assert all(item["review_state"] == "unknown" and item["source_span"] is None for item in page.items)
    assert "private" not in repr(page.items)


def test_admin_reader_keeps_alias_and_event_source_fields_explicitly_absent_when_missing() -> None:
    page = read_admin_identity_evidence(
        [
            IdentityLedger(
                {"editions": [{"gm_id": "edition-1", "evidence": [{"provider": "discogs", "external_id": "r1"}], "events": [{"type": "reattached"}]}]}
            )
        ],
        require_admin_payload={"type": "admin", "sub": "admin-1"},
    )
    alias, event = page.items
    assert alias["time"] == {
        "value": None,
        "meaning": "alias validity start; valid_to is the recorded validity end",
    }
    assert alias["source_ref"] == "discogs:r1"
    assert event["source"] == "unknown"
    assert event["source_ref"] is None
    assert event["time"] == {"value": None, "meaning": "ledger event date attributed to unknown"}


def test_admin_alias_refs_stay_null_when_provider_identifiers_are_unavailable() -> None:
    page = read_admin_identity_evidence(
        [IdentityLedger({"work": {"providers": [{"provider": None, "external_id": "r1", "valid_to": None}]}})],
        require_admin_payload={"type": "admin", "sub": "admin"},
    )
    assert len(page.items) == 1
    assert page.items[0]["source_ref"] is None
    assert page.items[0]["value"] == {"provider": None, "external_id": "r1", "valid_to": None}


def test_all_readers_report_truncation_and_validate_bounds() -> None:
    facts = [CatalogFact("release_details", f"r{i}", {"id": str(i)}) for i in range(3)]
    page = read_public_evidence(facts, limit=2)
    assert page.truncated is True
    assert len(page.items) == 2
    owner_page = read_owner_evidence(facts, trusted_owner_id="owner", limit=1)
    assert owner_page.truncated is True
    admin_page = read_admin_identity_evidence(
        [IdentityLedger({"editions": [{"gm_id": str(i), "evidence": [{"provider": "discogs", "external_id": str(i)}]}]}) for i in range(2)],
        require_admin_payload={"type": "admin", "sub": "admin"},
        limit=1,
    )
    assert admin_page.truncated is True
    for reader in (
        lambda: read_public_evidence([], limit=0),
        lambda: read_owner_evidence([], trusted_owner_id="owner", limit=101),
        lambda: read_admin_identity_evidence([], require_admin_payload={"type": "admin", "sub": "admin"}, limit=-1),
    ):
        with pytest.raises(ValueError, match="limit must be between"):
            reader()


def test_empty_sources_return_explicit_empty_untruncated_pages() -> None:
    public = read_public_evidence([OwnerObservation("owner", {}), IdentityLedger({})])
    owner = read_owner_evidence([OwnerObservation("other", {})], trusted_owner_id="owner")
    admin = read_admin_identity_evidence([CatalogFact("release_details", None, {})], require_admin_payload={"type": "admin", "sub": "admin"})
    assert public.items == owner.items == admin.items == ()
    assert public.truncated is owner.truncated is admin.truncated is False


def test_owner_reader_fails_closed_for_missing_principal_and_unbound_owner_rows() -> None:
    with pytest.raises(ValueError, match="authenticated principal"):
        read_owner_evidence([], trusted_owner_id="")
    with pytest.raises(ValueError, match="authenticated principal"):
        read_owner_evidence([], trusted_owner_id="  ")
    with pytest.raises(ValueError, match="owner_id must be bound"):
        OwnerObservation(" ", {"id": "unbound", "kind": "note", "value": "private"})


def test_conflicting_facts_for_one_trusted_subject_keep_both_source_refs() -> None:
    page = read_public_evidence(
        [
            CatalogFact("release_details", "record-alpha-42", {"id": "record-a", "name": "Title A"}, subject_ref="native-42"),
            CatalogFact("release_details", "record-beta-42", {"id": "record-b", "name": "Title B"}, subject_ref="native-42"),
        ]
    )
    titles = [item for item in page.items if item["field_ref"] == "release_details.name"]
    assert len(titles) == 2
    assert {item["subject_ref"] for item in titles} == {"native-42"}
    assert {item["source_ref"] for item in titles} == {"record-alpha-42", "record-beta-42"}
    assert {item["value"] for item in titles} == {"Title A", "Title B"}
    assert all("attribution unavailable" in item["source"] for item in titles)
    assert all(item["source_span"] is None and item["review_state"] == "unknown" for item in titles)


def test_projection_handles_missing_public_fields_and_incomplete_observation_rows() -> None:
    public = read_public_evidence(
        [
            CatalogFact("release_details", None, {"id": None, "name": None, "private": "ignored"}),
            CatalogFact("release_credit", None, {"owner_id": "ignored"}),
        ]
    )
    owner = read_owner_evidence(
        [
            OwnerObservation("owner-a", {"id": "only-ref", "created_at": None}),
            OwnerObservation("owner-a", {"kind": None, "value": None, "confidence": 0.5}),
        ],
        trusted_owner_id="owner-a",
    )
    assert public.items == ()
    assert len(owner.items) == 1
    assert owner.items[0]["field_ref"] == "observation.created_at"
    assert owner.items[0]["value"] is None
    assert owner.items[0]["source"] == "unknown"


def test_admin_projection_skips_malformed_and_empty_ledger_sections() -> None:
    page = read_admin_identity_evidence(
        [
            IdentityLedger({"work": [], "editions": [None, {"gm_id": "empty", "evidence": [None, {}], "events": "not-a-list"}]}),
            IdentityLedger({"work": {"providers": [None, {}], "events": [None, {}]}, "editions": None}),
        ],
        require_admin_payload={"type": "admin", "sub": "admin-1"},
    )
    assert page.items == ()
