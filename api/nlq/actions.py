"""NLQ action schemas and validation."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import structlog
from common.media import family_ids as _media_family_ids
from common.media import medium_ids as _media_medium_ids
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator


logger = structlog.get_logger(__name__)

_MAX_FIELD_LEN = 256

EntityType = Literal["artist", "label", "genre", "style", "release"]
PaneName = Literal["explore", "trends", "insights", "genres", "credits"]
FilterDimension = Literal["year", "genre", "label", "media"]


class _SeedEntity(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=_MAX_FIELD_LEN)]
    entity_type: EntityType


class SeedGraphAction(BaseModel):
    type: Literal["seed_graph"] = "seed_graph"
    entities: list[_SeedEntity]
    replace: bool = False


class HighlightPathAction(BaseModel):
    type: Literal["highlight_path"] = "highlight_path"
    nodes: list[Annotated[str, Field(min_length=1, max_length=_MAX_FIELD_LEN)]]


class FocusNodeAction(BaseModel):
    type: Literal["focus_node"] = "focus_node"
    name: Annotated[str, Field(min_length=1, max_length=_MAX_FIELD_LEN)]
    entity_type: EntityType


class FilterGraphAction(BaseModel):
    type: Literal["filter_graph"] = "filter_graph"
    by: FilterDimension
    value: Annotated[str | int | tuple[int, int], Field()]

    @model_validator(mode="after")
    def _validate_media_value(self) -> FilterGraphAction:
        """When ``by == "media"``, ``value`` must be a known family or medium id.

        Unlike the other filter dimensions (free-form year/genre/label text),
        a media filter drives the same ADR 0007 taxonomy the data tools
        validate against, so a hallucinated id is caught here too rather
        than silently reaching the UI as an unfilterable value.
        """
        if self.by == "media":
            known = frozenset(_media_family_ids()) | frozenset(_media_medium_ids())
            if not isinstance(self.value, str) or self.value not in known:
                msg = f"unknown media id for filter_graph: {self.value!r}. Expected a canonical media family or medium id (ADR 0007 taxonomy)."
                raise ValueError(msg)
        return self


class FindPathAction(BaseModel):
    type: Literal["find_path"] = "find_path"
    from_name: Annotated[str, Field(min_length=1, max_length=_MAX_FIELD_LEN, alias="from")]
    to_name: Annotated[str, Field(min_length=1, max_length=_MAX_FIELD_LEN, alias="to")]
    from_type: EntityType
    to_type: EntityType

    model_config = {"populate_by_name": True}


class ShowCreditsAction(BaseModel):
    type: Literal["show_credits"] = "show_credits"
    name: Annotated[str, Field(min_length=1, max_length=_MAX_FIELD_LEN)]
    entity_type: EntityType


class SwitchPaneAction(BaseModel):
    type: Literal["switch_pane"] = "switch_pane"
    pane: PaneName


class OpenInsightTileAction(BaseModel):
    type: Literal["open_insight_tile"] = "open_insight_tile"
    tile_id: Annotated[str, Field(min_length=1, max_length=_MAX_FIELD_LEN)]


class SetTrendRangeAction(BaseModel):
    type: Literal["set_trend_range"] = "set_trend_range"
    from_year: Annotated[str, Field(min_length=4, max_length=10, alias="from")]
    to_year: Annotated[str, Field(min_length=4, max_length=10, alias="to")]

    model_config = {"populate_by_name": True}


class SuggestFollowupsAction(BaseModel):
    type: Literal["suggest_followups"] = "suggest_followups"
    queries: list[Annotated[str, Field(min_length=1, max_length=_MAX_FIELD_LEN)]]


Action = Annotated[
    SeedGraphAction
    | HighlightPathAction
    | FocusNodeAction
    | FilterGraphAction
    | FindPathAction
    | ShowCreditsAction
    | SwitchPaneAction
    | OpenInsightTileAction
    | SetTrendRangeAction
    | SuggestFollowupsAction,
    Field(discriminator="type"),
]

_action_adapter: TypeAdapter[Action] = TypeAdapter(Action)


def parse_action(raw: dict[str, Any]) -> Action:
    """Parse and validate a single action. Raises ValidationError on failure."""
    return _action_adapter.validate_python(raw)


def parse_action_list(raw: list[dict[str, Any]]) -> list[Action]:
    """Parse a list of raw action dicts, dropping malformed entries with a warning."""
    parsed: list[Action] = []
    for item in raw:
        try:
            parsed.append(parse_action(item))
        except ValidationError as exc:
            logger.warning("⚠️ dropping malformed NLQ action", item=item, errors=exc.errors())
    return parsed
