"""The perf harness must send /api/expand the name the route matches on and flag empty results."""

import httpx

from performance.run_perftest import build_test_plan, response_is_empty, run_endpoint


def _plan() -> list[dict]:
    config = {"api_base_url": "http://api"}
    return build_test_plan(config, {"Solarstone": 42}, {"Armada": 7})


def test_expand_scenarios_send_names_not_ids() -> None:
    expand = {t["name"]: t for t in _plan() if t["name"].startswith("expand/")}
    assert expand["expand/artist/Solarstone/releases"]["params"]["node_id"] == "Solarstone"
    assert expand["expand/label/Armada/releases"]["params"]["node_id"] == "Armada"
    assert all(t["nonempty_key"] == "children" for t in expand.values())


def test_response_is_empty() -> None:
    assert response_is_empty(httpx.Response(200, json={"children": []}), "children")
    assert response_is_empty(httpx.Response(200, text="not json"), "children")
    assert not response_is_empty(httpx.Response(200, json={"children": [{"id": 1}]}), "children")
    assert not response_is_empty(httpx.Response(404, json={}), "children")


def test_run_endpoint_counts_empty_runs_as_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"children": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_endpoint(client, "expand/x", "http://api/api/expand", None, 2, nonempty_key="children")
    assert result["errors"] == 2
    assert result["empty_runs"] == 2
