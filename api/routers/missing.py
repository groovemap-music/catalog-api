"""First-party session-only transient Missing previews."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api.dependencies import require_user
from api.missing_preview import MissingSource, missing_preview
from api.pursuit_scope import TargetKind


router = APIRouter()
_source: Any = None


class MissingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_kind: TargetKind
    target_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    limit: int = Field(default=100, ge=1, le=200)


def configure(driver: Any, pool: Any) -> None:
    global _source
    _source = MissingSource(driver, pool) if driver is not None and pool is not None else None


@router.post("/api/user/missing/preview")
async def preview_missing(body: MissingRequest, request: Request, user: Annotated[dict[str, Any], Depends(require_user)]) -> dict[str, Any]:
    """Read a bounded Discogs artist/label projection using the session owner's copies."""
    if request.query_params:
        raise HTTPException(status_code=422, detail="Query controls are unsupported")
    if _source is None:
        raise HTTPException(status_code=503, detail="Missing preview unavailable")
    result = await missing_preview(_source, user["sub"], body.target_kind, body.target_id, body.limit)
    if result["sources"]["catalog"]["status"] == "unavailable":
        raise HTTPException(status_code=503, detail="Catalog preview unavailable")
    return result
