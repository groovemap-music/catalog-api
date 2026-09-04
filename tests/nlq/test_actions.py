"""Tests for NLQ action schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_seed_graph_action_validates() -> None:
    from api.nlq.actions import parse_action

    action = parse_action({"type": "seed_graph", "entities": [{"name": "Kraftwerk", "entity_type": "artist"}]})
    assert action.type == "seed_graph"
    assert action.entities[0].name == "Kraftwerk"
    assert action.entities[0].entity_type == "artist"


def test_switch_pane_action_validates() -> None:
    from api.nlq.actions import parse_action

    action = parse_action({"type": "switch_pane", "pane": "trends"})
    assert action.type == "switch_pane"
    assert action.pane == "trends"


def test_switch_pane_rejects_unknown_pane() -> None:
    from api.nlq.actions import parse_action

    with pytest.raises(ValidationError):
        parse_action({"type": "switch_pane", "pane": "not_a_real_pane"})


def test_unknown_action_type_raises() -> None:
    from api.nlq.actions import parse_action

    with pytest.raises(ValidationError):
        parse_action({"type": "time_travel", "year": 1999})


def test_seed_graph_entity_name_length_cap() -> None:
    from api.nlq.actions import parse_action

    with pytest.raises(ValidationError):
        parse_action({"type": "seed_graph", "entities": [{"name": "x" * 257, "entity_type": "artist"}]})


def test_filter_graph_action_accepts_media_family() -> None:
    """gm-catalog-api-be1.7 — by='media' with a family id (e.g. 'tape') validates."""
    from api.nlq.actions import parse_action

    action = parse_action({"type": "filter_graph", "by": "media", "value": "tape"})
    assert action.by == "media"
    assert action.value == "tape"


def test_filter_graph_action_accepts_media_medium() -> None:
    """gm-catalog-api-be1.7 — by='media' with a medium id (e.g. 'tape_cassette') validates."""
    from api.nlq.actions import parse_action

    action = parse_action({"type": "filter_graph", "by": "media", "value": "tape_cassette"})
    assert action.by == "media"
    assert action.value == "tape_cassette"


def test_filter_graph_action_rejects_unknown_media_id() -> None:
    """gm-catalog-api-be1.7 regression — the model bounce: by='media' with an id the
    taxonomy does not know must fail validation, not pass through silently."""
    from api.nlq.actions import parse_action

    with pytest.raises(ValidationError):
        parse_action({"type": "filter_graph", "by": "media", "value": "betamax_definitely_not_real"})


def test_parse_action_list_drops_malformed() -> None:
    from api.nlq.actions import parse_action_list

    raw = [
        {"type": "seed_graph", "entities": [{"name": "Kraftwerk", "entity_type": "artist"}]},
        {"type": "nonsense"},
        {"type": "focus_node", "name": "Kraftwerk", "entity_type": "artist"},
    ]
    actions = parse_action_list(raw)
    assert len(actions) == 2
    assert actions[0].type == "seed_graph"
    assert actions[1].type == "focus_node"
