"""Rate limits and work budgets for the expensive graph routes (gm-catalog-api-8fou).

Every value is read from the environment when it is used, so an operator changes one with a restart-free
override in tests and a plain environment variable in a deployment. A malformed value falls back to its
default rather than disabling the protection it sets.
"""

from __future__ import annotations

from os import getenv

from limits import parse


# --- Rate limits (slowapi / limits syntax, e.g. "10/minute"; several joined by ";") -------------------------

DEFAULT_RATE_LIMIT = "300/minute"
EXPLORE_RATE_LIMIT = "30/minute"
EXPAND_RATE_LIMIT = "60/minute"
TRENDS_RATE_LIMIT = "10/minute"
PATH_RATE_LIMIT = "6/minute"

_LIMIT_ENV = {
    "default": ("RATE_LIMIT_DEFAULT", DEFAULT_RATE_LIMIT),
    "explore": ("RATE_LIMIT_EXPLORE", EXPLORE_RATE_LIMIT),
    "expand": ("RATE_LIMIT_EXPAND", EXPAND_RATE_LIMIT),
    "trends": ("RATE_LIMIT_TRENDS", TRENDS_RATE_LIMIT),
    "path": ("RATE_LIMIT_PATH", PATH_RATE_LIMIT),
}


def rate_limit(name: str) -> str:
    """The limit string for ``name`` (default, explore, expand, trends, path); an unparsable override is ignored."""
    env_name, default = _LIMIT_ENV[name]
    raw = (getenv(env_name) or "").strip()
    if not raw:
        return default
    try:
        for part in raw.split(";"):
            parse(part.strip())
    except ValueError:
        return default
    return raw


# --- Path search ---------------------------------------------------------------------------------------------

DEFAULT_PATH_TIMEOUT_SECONDS = 10.0
DEFAULT_PATH_MAX_DEPTH = 8
# A vertex with more edges than this is a hub. 0 turns the guard off.
DEFAULT_PATH_HUB_DEGREE = 500_000
DEFAULT_TRENDS_LIVE_TIMEOUT_SECONDS = 8.0
DEFAULT_TRENDS_LIVE_CONCURRENCY = 1


def _env_float(name: str, default: float) -> float:
    try:
        value = float(getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(getenv(name, ""))
    except ValueError:
        return default
    return value if value >= minimum else default


def path_timeout_seconds() -> float:
    """Server-side budget for one shortestPath statement."""
    return _env_float("PATH_SEARCH_TIMEOUT_SECONDS", DEFAULT_PATH_TIMEOUT_SECONDS)


def path_max_depth(ceiling: int) -> int:
    """Deepest path a caller may ask for; never above ``ceiling``, the hard bound in the query layer."""
    return min(_env_int("PATH_SEARCH_MAX_DEPTH", DEFAULT_PATH_MAX_DEPTH, minimum=1), ceiling)


def path_hub_degree() -> int:
    """Edge count above which a path endpoint is a hub; 0 disables the guard."""
    return _env_int("PATH_HUB_DEGREE_THRESHOLD", DEFAULT_PATH_HUB_DEGREE)


# --- Trends live fallback --------------------------------------------------------------------------------------


def trends_live_timeout_seconds() -> float:
    """Server-side budget for the live traversal that runs when the stored series is missing or stale."""
    return _env_float("TRENDS_LIVE_TIMEOUT_SECONDS", DEFAULT_TRENDS_LIVE_TIMEOUT_SECONDS)


def trends_live_concurrency() -> int:
    """How many live trends traversals may run at once in this process."""
    return _env_int("TRENDS_LIVE_CONCURRENCY", DEFAULT_TRENDS_LIVE_CONCURRENCY, minimum=1)
