"""Contracts for disposable service containers used by the test harness."""

from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_integration_cleanup_removes_only_test_container_anonymous_volumes() -> None:
    script = (ROOT / "scripts" / "test-integration.sh").read_text()

    assert 'docker rm --force --volumes "${postgres_container}" "${neo4j_container}"' in script
    assert "docker volume prune" not in script
    assert "docker volume rm" not in script
