"""Pydantic models for the API service."""

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from common.identity import alias_sources, is_valid_alias_source
from pydantic import BaseModel, ConfigDict, Field, field_validator


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterRequest(BaseModel):
    """User registration request."""

    email: str
    password: str = Field(min_length=8, description="Password (minimum 8 characters)")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Validate and normalize email address."""
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email address")
        return v


class LoginRequest(BaseModel):
    """User login request."""

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        """Normalize email address to lowercase."""
        return v.strip().lower()


class LoginResponse(BaseModel):
    """User login response with JWT access token."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105  # nosec B105
    expires_in: int  # seconds until expiration


class UserResponse(BaseModel):
    """User information response."""

    id: UUID
    email: str
    is_active: bool
    created_at: datetime


class SnapshotNode(BaseModel):
    """A single node in a graph snapshot.

    ``id``/``type`` are bounded so a caller can't inflate the serialized
    snapshot payload with megabyte-sized strings per node — the node-count
    cap (`SnapshotStore.max_nodes`, enforced at save time) only bounds the
    number of nodes, not their size (groovemap-cu2.110). Real Discogs/
    MusicBrainz entity ids and type names are far shorter than these caps.
    """

    id: str = Field(max_length=128)
    type: str = Field(max_length=32)


class SnapshotRequest(BaseModel):
    """Request body for saving a graph snapshot."""

    nodes: list[SnapshotNode]
    center: SnapshotNode

    @field_validator("nodes")
    @classmethod
    def nodes_not_empty(cls, v: list[SnapshotNode]) -> list[SnapshotNode]:
        if not v:
            raise ValueError("nodes must not be empty")
        return v


class SnapshotResponse(BaseModel):
    """Response after saving a snapshot."""

    token: str
    url: str
    expires_at: str


class SnapshotRestoreResponse(BaseModel):
    """Response when restoring a snapshot."""

    nodes: list[SnapshotNode]
    center: SnapshotNode
    created_at: str


class PathNode(BaseModel):
    """A single node in a shortest-path result."""

    id: str
    name: str
    type: str
    rel: str | None = None  # relationship type leading TO this node (None for start node)


class PathResponse(BaseModel):
    """Response for GET /api/path."""

    found: bool
    length: int | None
    path: list[PathNode]


# --- Label DNA models ---


class GenreWeight(BaseModel):
    """A genre with its share of a label's catalog."""

    name: str
    count: int
    percentage: float


class StyleWeight(BaseModel):
    """A style with its share of a label's catalog."""

    name: str
    count: int
    percentage: float


class FormatWeight(BaseModel):
    """A physical/digital format with its share of a label's catalog.

    Deprecated: built from the raw ``Release.formats`` name list. Kept for one
    minor version alongside the family-grouped ``media`` profile
    (``MediaFamilyWeight``) — new consumers should read ``media`` instead.
    """

    name: str
    count: int
    percentage: float


class MediumWeight(BaseModel):
    """A single canonical medium (e.g. ``vinyl_12``) with its share within its family."""

    id: str
    label: str
    count: int
    percentage: float


class MediaFamilyWeight(BaseModel):
    """A media family (e.g. vinyl, optical, digital) with its share of a label's catalog.

    ``mediums`` is empty when the profile came from the ``Release.media_families``
    fallback (pre-cutover graph, no ``ISSUED_ON`` edges yet).
    """

    name: str
    count: int
    percentage: float
    mediums: list[MediumWeight]


class DecadeCount(BaseModel):
    """Release count for a single decade."""

    decade: int
    count: int
    percentage: float


class LabelDNA(BaseModel):
    """Full fingerprint for a record label."""

    label_id: str
    label_name: str
    release_count: int
    artist_count: int
    artist_diversity: float  # unique artists / releases (0-1 scale, higher = more diverse)
    active_years: list[int]  # sorted list of years with releases
    peak_decade: int | None  # decade with most releases
    prolificacy: float  # releases per active year
    genres: list[GenreWeight]
    styles: list[StyleWeight]
    formats: list[FormatWeight]  # deprecated — see MediaFamilyWeight docstring
    media: list[MediaFamilyWeight]
    decades: list[DecadeCount]


class SimilarLabel(BaseModel):
    """A label with its similarity score to a target label."""

    label_id: str
    label_name: str
    similarity: float  # cosine similarity 0-1
    release_count: int
    shared_genres: list[str]


class SimilarLabelsResponse(BaseModel):
    """Response for GET /api/label/{label_id}/similar."""

    label_id: str
    label_name: str
    similar: list[SimilarLabel]


class LabelCompareEntry(BaseModel):
    """One label's DNA in a side-by-side comparison."""

    dna: LabelDNA


class LabelCompareResponse(BaseModel):
    """Response for GET /api/label/dna/compare."""

    labels: list[LabelCompareEntry]


# ---------------------------------------------------------------------------
# Taste Fingerprint models
# ---------------------------------------------------------------------------


class HeatmapCell(BaseModel):
    """Single cell in a genre x decade heatmap."""

    genre: str
    decade: int
    count: int


class HeatmapResponse(BaseModel):
    """Genre x decade heatmap for a user's collection."""

    cells: list[HeatmapCell]
    total: int


class ObscurityScore(BaseModel):
    """How obscure a user's collection is (0 = mainstream, 1 = maximally obscure)."""

    score: float = Field(ge=0.0, le=1.0)
    median_collectors: float
    total_releases: int


class TasteDriftYear(BaseModel):
    """Top genre for a single year of additions."""

    year: str
    top_genre: str
    count: int


class BlindSpot(BaseModel):
    """A genre the user's favourite artists release in but the user hasn't collected."""

    genre: str
    artist_overlap: int
    example_release: str | None = None


class FingerprintResponse(BaseModel):
    """Full taste fingerprint combining all sub-queries."""

    heatmap: list[HeatmapCell]
    obscurity: ObscurityScore
    drift: list[TasteDriftYear]
    blind_spots: list[BlindSpot]
    peak_decade: int | None = None


# ---------------------------------------------------------------------------
# Recommender models
# ---------------------------------------------------------------------------


class SimilarArtist(BaseModel):
    """An artist with its similarity score to a target artist."""

    artist_id: str
    artist_name: str
    similarity: float  # weighted cosine similarity 0-1
    breakdown: dict[str, float]  # per-dimension scores: genre, style, label, collaborator
    release_count: int
    shared_genres: list[str]
    shared_labels: list[str]
    # ADR 0009: the native id beside the provider id, so a consumer can key on identity
    # GrooveMap owns. None when the alias table carries no valid alias for the artist.
    gm_id: str | None = None


class SimilarArtistsResponse(BaseModel):
    """Response for GET /api/recommend/similar/artist/{artist_id}."""

    artist_id: str
    artist_name: str
    similar: list[SimilarArtist]


class EntityRef(BaseModel):
    """Reference to a graph entity (artist, label, genre, style)."""

    id: str
    name: str
    type: str
    # Genre and style nodes are name-keyed rather than catalog entities, so they carry no
    # native id at all; for them this stays None even when the alias table is healthy.
    gm_id: str | None = None


class DiscoveryNode(BaseModel):
    """A discovered node from personalized graph traversal."""

    id: str
    name: str
    type: str
    score: float
    path: list[str]
    reason: str
    gm_id: str | None = None


class ExploreFromHereResponse(BaseModel):
    """Response for GET /api/recommend/explore/{entity_type}/{entity_id}."""

    model_config = ConfigDict(populate_by_name=True)

    from_entity: EntityRef = Field(alias="from")
    discoveries: list[DiscoveryNode]


class EnhancedRecommendation(BaseModel):
    """A release recommendation with multi-signal scoring."""

    id: str
    title: str | None = None
    artist: str | None = None
    label: str | None = None
    year: int | None = None
    genres: list[str] = Field(default_factory=list)
    score: float
    reasons: list[str] = Field(default_factory=list)
    gm_id: str | None = None


class EnhancedRecommendationsResponse(BaseModel):
    """Response for GET /api/user/recommendations?strategy=multi."""

    recommendations: list[EnhancedRecommendation]
    total: int
    strategy: str = "multi"


# --- Admin Models ---


class AdminLoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105  # nosec B105
    expires_in: int


# --- Password Reset Models ---


class ResetRequestModel(BaseModel):
    """Request to initiate a password reset."""

    email: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class ResetConfirmModel(BaseModel):
    """Request to confirm a password reset with a new password."""

    token: str
    new_password: str = Field(min_length=8, description="New password (minimum 8 characters)")


# --- Two-Factor Authentication Models ---


class TwoFactorSetupResponse(BaseModel):
    """Response from 2FA setup — contains secret, QR URI, and recovery codes."""

    secret: str
    otpauth_uri: str
    recovery_codes: list[str]


class TwoFactorCodeModel(BaseModel):
    """Request containing a 6-digit TOTP code."""

    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class TwoFactorVerifyModel(BaseModel):
    """Request to verify a TOTP code during login."""

    challenge_token: str
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class TwoFactorRecoveryModel(BaseModel):
    """Request to use a recovery code during login."""

    challenge_token: str
    code: str


class TwoFactorDisableModel(BaseModel):
    """Request to disable 2FA — requires current TOTP code and password."""

    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    password: str


class ChangePasswordRequest(BaseModel):
    """Request to change password while authenticated."""

    current_password: str
    new_password: str = Field(min_length=8, description="New password (minimum 8 characters)")


class ChallengeResponse(BaseModel):
    """Response when login requires 2FA — contains a challenge token."""

    requires_2fa: bool = True
    challenge_token: str


# ── App tokens (third-party app authorization) ────────────────────────────────


class MintAppTokenRequest(BaseModel):
    """Request body for POST /api/user/app-tokens."""

    name: str = Field(min_length=1, max_length=255, description='Human label, e.g. "GRUVAX kiosk"')
    scopes: list[str] = Field(min_length=1, description='Permission scopes, e.g. ["collection:read"]')


class MintAppTokenResponse(BaseModel):
    """Response from POST /api/user/app-tokens — plaintext returned ONCE."""

    id: UUID
    name: str
    scopes: list[str]
    token: str = Field(description="Plaintext token (dscg_…) — never recoverable")
    created_at: datetime


class AppTokenActive(BaseModel):
    """One active row from GET /api/user/app-tokens.active."""

    id: UUID
    name: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None


class AppTokenRevoked(BaseModel):
    """Tombstone row from GET /api/user/app-tokens.revoked."""

    id: UUID
    name: str
    revoked_at: datetime


class ListAppTokensResponse(BaseModel):
    """Response from GET /api/user/app-tokens."""

    active: list[AppTokenActive]
    revoked: list[AppTokenRevoked]


class ExtractionHistoryResponse(BaseModel):
    id: UUID
    triggered_by: UUID
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: float | None = None
    record_counts: dict[str, int] | None
    error_message: str | None
    extractor_version: str | None
    created_at: datetime


class ExtractionListResponse(BaseModel):
    extractions: list[ExtractionHistoryResponse]
    total: int
    offset: int
    limit: int


class ExtractionTriggerResponse(BaseModel):
    id: UUID
    status: str


class ProjectionTriggerResponse(BaseModel):
    """Response to POST /api/admin/identity/project — the gm_id projection job's job id."""

    id: UUID
    status: str


class ReattachTriggerResponse(BaseModel):
    """Response to POST /api/admin/identity/reattach — the re-attachment job's id and mode."""

    id: UUID
    status: str
    apply: bool


class DlqPurgeResponse(BaseModel):
    queue: str
    messages_purged: int


class AuditLogEntry(BaseModel):
    """A single audit log entry for admin actions."""

    id: UUID
    admin_id: UUID
    admin_email: str
    action: str
    target: str | None
    details: dict[str, Any] | None
    created_at: datetime


class AuditLogResponse(BaseModel):
    """Paginated audit log response."""

    entries: list[AuditLogEntry]
    total: int
    page: int
    page_size: int


# --- Admin Phase 2 Response Models ---


class DailyRegistration(BaseModel):
    """User registration counts per day."""

    date: str
    count: int


class WeeklyRegistration(BaseModel):
    """User registration counts per week."""

    week_start: str
    count: int


class MonthlyRegistration(BaseModel):
    """User registration counts per month."""

    month: str
    count: int


class RegistrationTimeSeries(BaseModel):
    """Time series of user registrations at multiple granularities."""

    daily: list[DailyRegistration]
    weekly: list[WeeklyRegistration]
    monthly: list[MonthlyRegistration]


class UserStatsResponse(BaseModel):
    """Response model for admin user statistics endpoint."""

    total_users: int
    active_7d: int
    active_30d: int
    oauth_connection_rate: float
    registrations: dict[str, Any]


class SyncPeriodStats(BaseModel):
    """Sync activity statistics for a time period."""

    total_syncs: int
    syncs_per_day: float
    avg_items_synced: float
    failure_rate: float
    total_failures: int


class SyncActivityResponse(BaseModel):
    """Response model for admin sync activity endpoint."""

    model_config = ConfigDict(populate_by_name=True)

    period_7d: SyncPeriodStats
    period_30d: SyncPeriodStats


class NodeCount(BaseModel):
    """Neo4j node label with count."""

    label: str
    count: int


class RelationshipCount(BaseModel):
    """Neo4j relationship type with count."""

    type: str
    count: int


class StoreSizes(BaseModel):
    """Storage size breakdown for the graph backend's admin panel.

    All four fields are formatted strings (e.g. "1.2 GB"), not raw byte counts. `nodes`,
    `relationships`, and `strings` are the JMX per-store breakdown Neo4j's backend reports;
    the PostgreSQL backend (`api/queries/admin_pg_queries.py`) has no equivalent split —
    `graph.artist`, `graph.by_artist`, and the rest are views and tables over the same four
    base tables, so there is no per-kind size to report — and returns `None` for each of the
    three, leaving only `total` populated.
    """

    total: str
    nodes: str | None
    relationships: str | None
    strings: str | None


class Neo4jStorage(BaseModel):
    """Neo4j storage utilization details."""

    status: str
    nodes: list[NodeCount]
    relationships: list[RelationshipCount]
    store_sizes: StoreSizes | None


class TableSize(BaseModel):
    """PostgreSQL table size details."""

    name: str
    row_count: int
    size: str
    index_size: str


class PostgresStorage(BaseModel):
    """PostgreSQL storage utilization details."""

    status: str
    tables: list[TableSize]
    total_size: str


class RedisKeyPrefix(BaseModel):
    """Redis key prefix with count."""

    prefix: str
    count: int


class RedisStorage(BaseModel):
    """Redis storage utilization details."""

    status: str
    memory_used: str
    memory_peak: str
    total_keys: int
    keys_by_prefix: list[RedisKeyPrefix]


class StorageSourceError(BaseModel):
    """Error response for a storage source that could not be queried."""

    status: str = "error"
    error: str


class StorageResponse(BaseModel):
    """Response model for admin storage utilization endpoint."""

    neo4j: dict[str, Any]
    postgresql: dict[str, Any]
    redis: dict[str, Any]


class RaritySignal(BaseModel):
    """A single rarity signal score and its weight."""

    score: float
    weight: float


class RarityResponse(BaseModel):
    """Full rarity breakdown for a single release."""

    release_id: int
    title: str
    artist: str
    year: int | None
    rarity_score: float
    tier: str
    hidden_gem_score: float | None
    # Canonical media family ids the release covers (ADR 0007).
    media_families: list[str] = []
    # Family extension module id → the signals it contributed. Empty when none applied.
    family_signals: dict[str, dict[str, float]] = {}
    # Weights are renormalised over the signals this release actually has, so they sum to 1.0.
    # The deprecated `format_rarity` entry carries weight 0.0.
    breakdown: dict[str, RaritySignal]


class RarityListItem(BaseModel):
    """A release in a rarity list (leaderboard, artist, label)."""

    release_id: int
    title: str
    artist: str
    year: int | None
    rarity_score: float
    tier: str
    hidden_gem_score: float | None = None


class RarityListResponse(BaseModel):
    """Paginated list of rarity-scored releases."""

    items: list[RarityListItem]
    total: int
    page: int
    page_size: int


# --- Metrics History models (Phase 3) ---


class QueueHistoryResponse(BaseModel):
    """Response for GET /api/admin/queues/history."""

    model_config = ConfigDict(extra="forbid")

    range: str
    granularity: str
    queues: dict[str, Any]
    dlq_summary: dict[str, Any]


class HealthHistoryResponse(BaseModel):
    """Response for GET /api/admin/health/history."""

    model_config = ConfigDict(extra="forbid")

    range: str
    granularity: str
    services: dict[str, Any]
    api_endpoints: dict[str, Any]


# --- Media mapping coverage models (ADR 0007) ---


class UnmappedMediaName(BaseModel):
    """One raw provider name the media taxonomy did not map, and its release count."""

    model_config = ConfigDict(extra="forbid")

    # Which of the block's two `unmapped` lists the name came from.
    kind: str = Field(description='Either "format" (a provider format name) or "description" (a qualifier).')
    name: str = Field(description="The raw provider name, exactly as the record carries it.")
    releases: int = Field(description="How many media-tagged releases carry this name.")


class UnmappedMediaResponse(BaseModel):
    """Response for GET /api/admin/media/unmapped."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    media_tagged_releases: int
    releases_with_unmapped: int
    # releases_with_unmapped / media_tagged_releases, rounded to 4 places; 0.0 when nothing is tagged.
    unmapped_rate: float
    limit: int
    top_unmapped: list[UnmappedMediaName]


# ── Credits & Provenance models ──────────────────────────────────────────────


class CreditEntry(BaseModel):
    """A single credit on a release."""

    release_id: str
    title: str
    year: int | None = None
    role: str
    category: str
    artists: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class PersonCreditsResponse(BaseModel):
    """All credits for a person, grouped by role."""

    name: str
    total_credits: int
    credits: list[CreditEntry]


class TimelineEntry(BaseModel):
    """Year-by-year credit activity entry."""

    year: int
    category: str
    count: int


class PersonTimelineResponse(BaseModel):
    """Timeline of a person's credit activity."""

    name: str
    timeline: list[TimelineEntry]


class ReleaseCreditEntry(BaseModel):
    """A credited person on a release."""

    name: str
    role: str
    category: str
    artist_id: str | None = None
    artist_name: str | None = None


class ReleaseCreditsResponse(BaseModel):
    """Full credits breakdown for a release."""

    release_id: str
    credits: list[ReleaseCreditEntry]


class LeaderboardEntry(BaseModel):
    """A person in the role leaderboard."""

    name: str
    credit_count: int


class RoleLeaderboardResponse(BaseModel):
    """Top credited people for a given role category."""

    category: str
    entries: list[LeaderboardEntry]


class SharedCreditEntry(BaseModel):
    """A release where two people are both credited."""

    release_id: str
    title: str
    year: int | None = None
    person1_role: str
    person2_role: str
    artists: list[str] = Field(default_factory=list)


class SharedCreditsResponse(BaseModel):
    """Releases where two people are both credited."""

    person1: str
    person2: str
    shared_releases: list[SharedCreditEntry]


class ConnectionEntry(BaseModel):
    """A person connected through shared releases."""

    name: str
    shared_count: int


class PersonConnectionsResponse(BaseModel):
    """People connected through shared releases."""

    name: str
    connections: list[ConnectionEntry]


class PersonAutocompleteEntry(BaseModel):
    """Autocomplete result for a person."""

    name: str
    score: float


class PersonProfileResponse(BaseModel):
    """Summary profile for a credited person."""

    name: str
    total_credits: int
    categories: list[str] = Field(default_factory=list)
    first_year: int | None = None
    last_year: int | None = None
    artist_id: str | None = None
    artist_name: str | None = None
    role_breakdown: list[dict[str, Any]] = Field(default_factory=list)


# --- Observation Models (ADR 0009) ---


class CreateObservationRequest(BaseModel):
    """Request body for POST /api/user/copies/{copy_id}/observations.

    An observation is user-captured evidence about a copy the caller holds — a matrix
    inscription, a grading, a purchase price. `source` separates a fact a person asserted
    from one a matching heuristic proposed, so it is validated against the identity
    vocabulary rather than accepted free-form.
    """

    kind: str = Field(min_length=1, max_length=100, description='What is being observed, e.g. "matrix" or "grading"')
    value: str = Field(min_length=1, description="The observed value, as the user recorded it")
    source: str = Field(description=f"Who asserted it — one of: {', '.join(alias_sources())}")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, description="Optional confidence in the observation, 0-1")
    observed_at: datetime | None = Field(default=None, description="When it was observed; defaults to now")

    @field_validator("kind", "value")
    @classmethod
    def strip_text(cls, v: str) -> str:
        """Trim surrounding whitespace and reject a value that was only whitespace."""
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        """Reject a source outside the closed identity vocabulary."""
        v = v.strip().lower()
        if not is_valid_alias_source(v):
            raise ValueError(f"Unknown source {v!r}; must be one of: {', '.join(alias_sources())}")
        return v


class ObservationResponse(BaseModel):
    """One observation row, as both observation endpoints return it."""

    id: UUID
    owned_copy_id: UUID
    kind: str
    value: str
    source: str
    confidence: float | None = None
    observed_at: datetime
    created_at: datetime


# ---------------------------------------------------------------------------
# First-party activity, consent, and erasure (ADR 0010)
# ---------------------------------------------------------------------------

# The four outcomes a client may report against a recommendation it was shown. Everything
# else in the version 1 vocabulary is written server side from the surface that produced
# it, so the closed set here is what keeps a client from minting, say, a consent event.
RECOMMENDATION_OUTCOMES: tuple[str, ...] = (
    "recommendation.opened",
    "recommendation.saved",
    "recommendation.dismissed",
    "recommendation.hidden",
)

# The same four outcomes, for the `fit` surface. `fit.shown` is excluded for the same
# reason `recommendation.shown` is: it is written server side alongside the impression
# (see `api.routers.fit._stamp_impression`), never client-reported.
FIT_OUTCOMES: tuple[str, ...] = (
    "fit.opened",
    "fit.saved",
    "fit.dismissed",
    "fit.hidden",
)

# The full set of event types a client may report through POST /api/activity/events,
# across every surface that has client-reportable outcomes.
CLIENT_REPORTABLE_OUTCOMES: tuple[str, ...] = RECOMMENDATION_OUTCOMES + FIT_OUTCOMES


class ActivityOutcomeRequest(BaseModel):
    """Request body for POST /api/activity/events.

    `item_id` is required alongside `impression_id` because the published
    `impression_outcome` payload requires both and names no other key.
    """

    event_type: str = Field(description=f"One of: {', '.join(CLIENT_REPORTABLE_OUTCOMES)}")
    impression_id: UUID = Field(description="The impression the outcome is reported against")
    item_id: UUID = Field(description="The native id of the item the impression showed")

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        """Reject any type outside the client-reportable outcomes."""
        v = v.strip()
        if v not in CLIENT_REPORTABLE_OUTCOMES:
            raise ValueError(f"Unknown event_type {v!r}; must be one of: {', '.join(CLIENT_REPORTABLE_OUTCOMES)}")
        return v


class ConsentUpdateRequest(BaseModel):
    """Request body for PUT /api/user/consent/{purpose}."""

    granted: bool = Field(description="True to grant the purpose, false to revoke it")


class ErasureRequest(BaseModel):
    """Request body for POST /api/user/erasure.

    Erasure is irreversible across every store, so it is re-authenticated rather than
    taken on the bearer token alone: the current password always, and the current TOTP
    code as well when the account has 2FA enabled.
    """

    password: str = Field(description="The caller's current password")
    code: str | None = Field(default=None, pattern=r"^\d{6}$", description="Current TOTP code, required when 2FA is enabled")


# ---------------------------------------------------------------------------
# CrateFit — the item-in-hand fit profile
# ---------------------------------------------------------------------------


class FitEvidenceItem(BaseModel):
    """One structured fact behind a component's score, paired with its evidence sentence.

    The pairing is by position: entry ``i`` of a component's ``evidence_items`` is exactly
    what ``api.fit._render_entry`` turned into the string at position ``i`` of that
    component's ``evidence``, so a consumer that wants to key on the claim rather than
    parse the sentence reads the same fact the sentence states, never a second guess at it.
    """

    dimension: str = Field(description="The facet the claim is about: artist, label, genre, style, release, or similar")
    entity: str = Field(description="The name or id of the thing the claim is about")
    kind: str = Field(description="The shape of the claim: shared, unheld, thread, duplicate, bridge, or a component-specific kind")
    count: int | None = Field(default=None, description="The collector's held count for this facet, when the claim is about a holding")
    release_id: str | None = Field(default=None, description="The matched release id, when the claim is about a specific held release")
    detail: str | None = Field(default=None, description="Extra text a few claim kinds need to complete their sentence, e.g. a shared media family")


class FitComponent(BaseModel):
    """One dimension of a fit answer: a score in [0, 1] and the facts behind it."""

    score: float = Field(ge=0.0, le=1.0, description="This dimension's score, 0 to 1")
    evidence: list[str] = Field(default_factory=list, description="Facts about the caller's own collection that produced the score")
    evidence_items: list[FitEvidenceItem] = Field(
        default_factory=list, description="The same facts as `evidence`, structured, and capped by the same evidence limit"
    )


class FitComponents(BaseModel):
    """The five dimensions a fit answer decomposes into.

    Closed rather than a free dictionary: the decomposition is the product, and a consumer
    that renders five named panes should break loudly if a version ever drops one.
    """

    affinity: FitComponent
    novelty: FitComponent
    bridge: FitComponent
    depth: FitComponent
    redundancy: FitComponent


class FitRarity(BaseModel):
    """The precomputed rarity of the candidate, read from the insights tables."""

    score: float | None = None
    tier: str | None = None


class FitRelease(BaseModel):
    """The release a fit profile is about, as the caller needs to recognise it."""

    id: str
    # ADR 0009: the native id beside the Discogs id, so a client reports outcomes against
    # identity GrooveMap owns. None when the alias table carries no alias for the release.
    gm_id: str | None = None
    title: str | None = None
    artist: str | None = None
    year: int | None = None
    media_families: list[str] = Field(default_factory=list)
    # Read from `insights.release_rarity`, never computed on the request path, and never an
    # input to any v0 component: rarity is a fact about the record, fit is a fact about the
    # record *and this collector*, and conflating them would make a common record the
    # collector obviously wants look like a worse buy than a rare one they do not.
    rarity: FitRarity | None = None


class FitProfile(BaseModel):
    """Response for GET /api/fit/release/{release_id}."""

    release: FitRelease
    fit: float = Field(ge=0.0, le=1.0, description="The combined fit, 0 to 1")
    components: FitComponents
    confidence: str = Field(description="How the candidate was identified: 'exact' or 'master'")
    policy_id: str
    fit_version: str
    # Minted per request served, after the cached body is read, so a client can report an
    # outcome against the showing it actually saw. None when the release has no native id
    # or the impression could not be written.
    impression_id: str | None = None


class LookupRelease(BaseModel):
    """One release an identifier resolved to, as the caller needs to recognise it.

    ``source`` names the catalog the row came from rather than the catalog the identifier
    belongs to: a barcode is printed on the object, and both catalogs describe the same
    object, so one lookup can legitimately return a Discogs row and a MusicBrainz row for
    the same pressing.
    """

    id: str
    source: str = Field(description="The catalog the row came from: 'discogs' or 'musicbrainz'")
    title: str | None = None
    artist: str | None = None
    year: int | None = None
    media_families: list[str] = Field(default_factory=list)


class LookupResponse(BaseModel):
    """Response for GET /api/lookup/{provider}/{value} (ADR 0011)."""

    provider: str = Field(description="The alias namespace the value was resolved under")
    value: str = Field(description="The value exactly as the caller supplied it")
    normalized: str = Field(description="The value under the namespace's declared normalization")
    # ADR 0009: the identity the alias resolved to. Always present — a response is only
    # returned once a valid alias row named a native id.
    gm_id: str
    releases: list[LookupRelease] = Field(default_factory=list)
