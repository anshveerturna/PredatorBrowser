from pathlib import Path

from app.core.v2.control_plane_store import ControlPlaneStore


def test_control_plane_store_quota_and_rate(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    store = ControlPlaneStore(db_path=str(db))

    store.set_quota("tenant-1", {"max_actions_per_minute": 7, "max_concurrent_sessions": 2, "max_artifact_bytes": 1000, "max_step_tokens": 500})
    quota = store.get_quota("tenant-1")
    assert quota is not None
    assert quota["max_actions_per_minute"] == 7

    store.register_action("tenant-1", ts=100.0)
    store.register_action("tenant-1", ts=120.0)
    assert store.count_recent_actions("tenant-1", since_ts=80.0) == 2


def test_control_plane_store_session_lease(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    store = ControlPlaneStore(db_path=str(db))

    ok = store.acquire_session_lease("tenant-1", "wf-1", "owner-a", lease_ttl_seconds=30)
    assert ok is True

    blocked = store.acquire_session_lease("tenant-1", "wf-1", "owner-b", lease_ttl_seconds=30)
    assert blocked is False

    assert store.count_active_sessions("tenant-1", lease_ttl_seconds=30) == 1
    store.release_session_lease("wf-1", "owner-a")
    assert store.count_active_sessions("tenant-1", lease_ttl_seconds=30) == 0


def test_control_plane_store_circuit_tables(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    store = ControlPlaneStore(db_path=str(db))

    snap = store.get_circuit("example.com")
    assert snap.state == "closed"

    store.set_circuit("example.com", "open", opened_at=50.0)
    snap2 = store.get_circuit("example.com")
    assert snap2.state == "open"

    store.add_circuit_failure("example.com", ts=60.0)
    store.add_circuit_failure("example.com", ts=70.0)
    assert store.count_circuit_failures("example.com", since_ts=55.0) == 2

    assert "example.com" in store.list_circuit_domains()
