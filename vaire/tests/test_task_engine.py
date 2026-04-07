"""Tests for the task engine."""

import json
from unittest.mock import MagicMock

import pytest

from vaire.task_engine import VALID_PRIORITIES, VALID_STATUSES, TaskEngine


@pytest.fixture
def settings(tmp_path):
    s = MagicMock()
    s.task_data_path_resolved = tmp_path / "tasks.json"
    s.TASK_HEARTBEAT_TTL = 30
    s.task_create_allowed_list = []  # empty = unrestricted
    return s


@pytest.fixture
def engine(settings):
    return TaskEngine(settings)


class TestInit:
    def test_creates_empty_store(self, engine, settings):
        assert settings.task_data_path_resolved.exists()
        data = json.loads(settings.task_data_path_resolved.read_text())
        assert data["schema_version"] == 1
        assert data["version"] == 0
        assert data["tasks"] == {}

    def test_loads_existing_file(self, settings):
        store = {
            "schema_version": 1,
            "version": 5,
            "next_id": 10,
            "tasks": {"TASK-001": {"title": "test", "status": "open"}},
        }
        settings.task_data_path_resolved.write_text(json.dumps(store))
        engine = TaskEngine(settings)
        assert engine.version == 5
        result = engine.get_task("TASK-001")
        assert result is not None
        assert result["title"] == "test"


class TestCreateTask:
    def test_basic_create(self, engine):
        result = engine.create_task(
            agent_id="vale", host="localhost",
            title="Test task", role="dev",
        )
        assert result["id"] == "TASK-001"
        assert result["status"] == "open"
        assert result["title"] == "Test task"
        assert result["role"] == "dev"
        assert result["priority"] == "medium"

    def test_increments_id(self, engine):
        r1 = engine.create_task(
            agent_id="vale", host="h", title="First", role="dev"
        )
        r2 = engine.create_task(
            agent_id="vale", host="h", title="Second", role="dev"
        )
        assert r1["id"] == "TASK-001"
        assert r2["id"] == "TASK-002"

    def test_invalid_priority_rejected(self, engine):
        with pytest.raises(ValueError, match="Invalid priority"):
            engine.create_task(
                agent_id="vale", host="h",
                title="Bad", role="dev", priority="urgent",
            )

    def test_empty_title_rejected(self, engine):
        with pytest.raises(ValueError, match="title"):
            engine.create_task(
                agent_id="vale", host="h", title="", role="dev",
            )

    def test_prefix_authorization(self, settings):
        settings.task_create_allowed_list = ["groomer-", "vale"]
        engine = TaskEngine(settings)
        # Allowed prefix
        result = engine.create_task(
            agent_id="vale-main", host="h", title="OK", role="dev",
        )
        assert result["id"] == "TASK-001"
        # Denied prefix
        with pytest.raises(PermissionError, match="not authorized"):
            engine.create_task(
                agent_id="rando", host="h", title="Nope", role="dev",
            )

    def test_empty_allowed_list_permits_all(self, engine):
        """Verification fix P1-1: empty list = unrestricted."""
        result = engine.create_task(
            agent_id="anyone", host="h", title="Open", role="dev",
        )
        assert result["id"] == "TASK-001"

    def test_acceptance_criteria_validation(self, engine):
        result = engine.create_task(
            agent_id="vale", host="h", title="AC test", role="dev",
            acceptance_criteria=[{"text": "Do X"}, {"text": "Do Y"}],
        )
        assert len(result["acceptance_criteria"]) == 2
        assert result["acceptance_criteria"][0]["text"] == "Do X"
        assert result["acceptance_criteria"][0]["done"] is False

    def test_bad_criteria_rejected(self, engine):
        with pytest.raises(ValueError, match="text"):
            engine.create_task(
                agent_id="vale", host="h", title="Bad AC", role="dev",
                acceptance_criteria=[{"wrong_key": "x"}],
            )

    def test_none_defaults_not_shared(self, engine):
        """Verification fix P3b-1: mutable default arguments."""
        r1 = engine.create_task(
            agent_id="vale", host="h", title="A", role="dev",
        )
        r2 = engine.create_task(
            agent_id="vale", host="h", title="B", role="dev",
        )
        assert r1["depends_on"] is not r2["depends_on"]

    def test_deep_copy_returned(self, engine):
        """Verification fix P3a-2: shallow copy leaks."""
        result = engine.create_task(
            agent_id="vale", host="h", title="Deep", role="dev",
            acceptance_criteria=[{"text": "X"}],
        )
        result["acceptance_criteria"][0]["done"] = True
        # Internal state should not be affected
        internal = engine.get_task(result["id"])
        assert internal["acceptance_criteria"][0]["done"] is False


class TestClaimTask:
    def test_claim_open_task(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="T", role="dev",
        )
        result = engine.claim_task(
            task_id="TASK-001", agent_id="worker-1", host="h",
        )
        assert result["status"] == "in_progress"
        assert result["agent"]["claimed_by"] == "worker-1"

    def test_cannot_claim_nonexistent(self, engine):
        with pytest.raises(KeyError):
            engine.claim_task(
                task_id="TASK-999", agent_id="w", host="h",
            )

    def test_cannot_claim_done_task(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="T", role="dev",
        )
        engine.claim_task(
            task_id="TASK-001", agent_id="w", host="h",
        )
        engine.complete_task(
            task_id="TASK-001", agent_id="w", host="h",
        )
        with pytest.raises(ValueError, match="already done"):
            engine.claim_task(
                task_id="TASK-001", agent_id="w2", host="h",
            )

    def test_one_at_a_time(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="T1", role="dev",
        )
        engine.create_task(
            agent_id="vale", host="h", title="T2", role="dev",
        )
        engine.claim_task(
            task_id="TASK-001", agent_id="worker", host="h",
        )
        with pytest.raises(PermissionError, match="already holds"):
            engine.claim_task(
                task_id="TASK-002", agent_id="worker", host="h",
            )

    def test_cannot_claim_held_task(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="T", role="dev",
        )
        engine.claim_task(
            task_id="TASK-001", agent_id="w1", host="h",
        )
        with pytest.raises(PermissionError, match="held by"):
            engine.claim_task(
                task_id="TASK-001", agent_id="w2", host="h",
            )


class TestUpdateTask:
    @pytest.fixture
    def claimed_engine(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="T", role="dev",
            acceptance_criteria=[{"text": "X", "id": 1}],
        )
        engine.claim_task(
            task_id="TASK-001", agent_id="worker", host="h",
        )
        return engine

    def test_basic_update(self, claimed_engine):
        result = claimed_engine.update_task(
            task_id="TASK-001", agent_id="worker", host="h",
            notes="progress note", progress=50,
        )
        assert result["agent"]["progress_pct"] == 50

    def test_non_owner_rejected(self, claimed_engine):
        with pytest.raises(PermissionError, match="does not own"):
            claimed_engine.update_task(
                task_id="TASK-001", agent_id="intruder", host="h",
            )

    def test_progress_clamped(self, claimed_engine):
        result = claimed_engine.update_task(
            task_id="TASK-001", agent_id="worker", host="h",
            progress=150,
        )
        assert result["agent"]["progress_pct"] == 100

    def test_progress_none_no_change(self, claimed_engine):
        """Verification fix P3b-5: None means no change."""
        claimed_engine.update_task(
            task_id="TASK-001", agent_id="worker", host="h",
            progress=42,
        )
        result = claimed_engine.update_task(
            task_id="TASK-001", agent_id="worker", host="h",
            notes="no progress change",
        )
        assert result["agent"]["progress_pct"] == 42

    def test_criteria_done_validates_ids(self, claimed_engine):
        result = claimed_engine.update_task(
            task_id="TASK-001", agent_id="worker", host="h",
            criteria_done=[1, 999],  # 999 doesn't exist
        )
        ac = result["acceptance_criteria"]
        assert ac[0]["done"] is True

    def test_files_dedup(self, claimed_engine):
        claimed_engine.update_task(
            task_id="TASK-001", agent_id="worker", host="h",
            files=["a.py", "b.py"],
        )
        result = claimed_engine.update_task(
            task_id="TASK-001", agent_id="worker", host="h",
            files=["b.py", "c.py"],
        )
        assert result["agent"]["files_touched"] == ["a.py", "b.py", "c.py"]


class TestCompleteTask:
    def test_complete(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="T", role="dev",
        )
        engine.claim_task(
            task_id="TASK-001", agent_id="worker", host="h",
        )
        result = engine.complete_task(
            task_id="TASK-001", agent_id="worker", host="h",
            result="Done well",
        )
        assert result["status"] == "done"
        assert result["result"] == "Done well"
        assert result["agent"] is None
        assert result["completed_at"] is not None

    def test_non_owner_rejected(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="T", role="dev",
        )
        engine.claim_task(
            task_id="TASK-001", agent_id="worker", host="h",
        )
        with pytest.raises(PermissionError):
            engine.complete_task(
                task_id="TASK-001", agent_id="intruder", host="h",
            )


class TestReleaseTask:
    def test_release(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="T", role="dev",
        )
        engine.claim_task(
            task_id="TASK-001", agent_id="worker", host="h",
        )
        result = engine.release_task(
            task_id="TASK-001", agent_id="worker", host="h",
            reason="wrong role",
        )
        assert result["status"] == "open"
        assert result["agent"] is None


class TestListTasks:
    def test_filter_by_status(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="Open", role="dev",
        )
        engine.create_task(
            agent_id="vale", host="h", title="Also open", role="builder",
        )
        engine.claim_task(
            task_id="TASK-001", agent_id="w", host="h",
        )
        open_tasks = engine.list_tasks(status="open")
        assert len(open_tasks) == 1
        assert open_tasks[0]["title"] == "Also open"

    def test_filter_by_role(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="Dev", role="dev",
        )
        engine.create_task(
            agent_id="vale", host="h", title="Builder", role="builder",
        )
        dev_tasks = engine.list_tasks(role="dev")
        assert len(dev_tasks) == 1
        assert dev_tasks[0]["role"] == "dev"


class TestVersioning:
    def test_version_increments(self, engine):
        assert engine.version == 0
        engine.create_task(
            agent_id="vale", host="h", title="T", role="dev",
        )
        assert engine.version == 1

    def test_snapshot_deep_copy(self, engine):
        engine.create_task(
            agent_id="vale", host="h", title="T", role="dev",
        )
        snap = engine.task_cache_snapshot
        snap["tasks"]["TASK-001"]["title"] = "MUTATED"
        # Internal state should be unchanged
        assert engine.get_task("TASK-001")["title"] == "T"


class TestTaskSyncMerge:
    """Test TaskSyncThread._merge logic."""

    @pytest.fixture
    def sync(self, engine):
        from vaire.task_engine import TaskSyncThread
        gitlab = MagicMock()
        return TaskSyncThread(engine, gitlab, engine._settings)

    def _store(self, **tasks):
        """Build a task store dict."""
        return {
            "schema_version": 1,
            "version": 1,
            "next_id": 10,
            "tasks": tasks,
        }

    def _task(self, title="T", status="open", **kw):
        return {"title": title, "status": status, "history": [], **kw}

    def test_local_only_included(self, sync):
        local = self._store(T1=self._task("Local"))
        remote = self._store()
        merged = sync._merge(local, remote)
        assert "T1" in merged["tasks"]
        assert merged["tasks"]["T1"]["title"] == "Local"

    def test_remote_only_included(self, sync):
        local = self._store()
        remote = self._store(T1=self._task("Remote"))
        merged = sync._merge(local, remote)
        assert "T1" in merged["tasks"]
        assert merged["tasks"]["T1"]["title"] == "Remote"

    def test_title_remote_wins(self, sync):
        local = self._store(T1=self._task("Old Title"))
        remote = self._store(T1=self._task("New Title"))
        merged = sync._merge(local, remote)
        assert merged["tasks"]["T1"]["title"] == "New Title"

    def test_priority_remote_wins(self, sync):
        local = self._store(T1=self._task(priority="low"))
        remote = self._store(T1=self._task(priority="critical"))
        merged = sync._merge(local, remote)
        assert merged["tasks"]["T1"]["priority"] == "critical"

    def test_done_is_terminal(self, sync):
        local = self._store(T1=self._task(status="in_progress"))
        remote = self._store(T1=self._task(status="done"))
        merged = sync._merge(local, remote)
        assert merged["tasks"]["T1"]["status"] == "done"

    def test_done_terminal_reverse(self, sync):
        local = self._store(T1=self._task(status="done"))
        remote = self._store(T1=self._task(status="open"))
        merged = sync._merge(local, remote)
        assert merged["tasks"]["T1"]["status"] == "done"

    def test_more_progressed_wins(self, sync):
        local = self._store(T1=self._task(status="open"))
        remote = self._store(T1=self._task(status="in_progress"))
        merged = sync._merge(local, remote)
        assert merged["tasks"]["T1"]["status"] == "in_progress"

    def test_on_hold_vs_open(self, sync):
        local = self._store(T1=self._task(status="on_hold"))
        remote = self._store(T1=self._task(status="open"))
        merged = sync._merge(local, remote)
        assert merged["tasks"]["T1"]["status"] == "on_hold"

    def test_agent_newer_heartbeat_wins(self, sync):
        local = self._store(T1=self._task(
            agent={"claimed_by": "a1", "heartbeat": "2026-01-01T00:00:00"},
        ))
        remote = self._store(T1=self._task(
            agent={"claimed_by": "a2", "heartbeat": "2026-06-01T00:00:00"},
        ))
        merged = sync._merge(local, remote)
        assert merged["tasks"]["T1"]["agent"]["claimed_by"] == "a2"

    def test_agent_local_heartbeat_wins(self, sync):
        local = self._store(T1=self._task(
            agent={"claimed_by": "a1", "heartbeat": "2026-06-01T00:00:00"},
        ))
        remote = self._store(T1=self._task(
            agent={"claimed_by": "a2", "heartbeat": "2026-01-01T00:00:00"},
        ))
        merged = sync._merge(local, remote)
        assert merged["tasks"]["T1"]["agent"]["claimed_by"] == "a1"

    def test_history_union_dedupe(self, sync):
        h1 = {"ts": "2026-01-01T00:00:00", "action": "created", "by": "vale"}
        h2 = {"ts": "2026-01-02T00:00:00", "action": "claimed", "by": "w1"}
        h3 = {"ts": "2026-01-03T00:00:00", "action": "completed", "by": "w1"}
        local = self._store(T1=self._task(history=[h1, h2]))
        remote = self._store(T1=self._task(history=[h1, h3]))
        merged = sync._merge(local, remote)
        assert len(merged["tasks"]["T1"]["history"]) == 3
        # Sorted by ts
        ts_list = [e["ts"] for e in merged["tasks"]["T1"]["history"]]
        assert ts_list == sorted(ts_list)

    def test_acceptance_criteria_local_wins(self, sync):
        local = self._store(T1=self._task(
            acceptance_criteria=[{"id": 1, "text": "X", "done": True}],
        ))
        remote = self._store(T1=self._task(
            acceptance_criteria=[{"id": 1, "text": "X", "done": False}],
        ))
        merged = sync._merge(local, remote)
        assert merged["tasks"]["T1"]["acceptance_criteria"][0]["done"] is True

    def test_version_max_plus_one(self, sync):
        local = {"schema_version": 1, "version": 5, "next_id": 10, "tasks": {}}
        remote = {"schema_version": 1, "version": 8, "next_id": 12, "tasks": {}}
        merged = sync._merge(local, remote)
        assert merged["version"] == 9

    def test_next_id_max(self, sync):
        local = {"schema_version": 1, "version": 1, "next_id": 5, "tasks": {}}
        remote = {"schema_version": 1, "version": 1, "next_id": 12, "tasks": {}}
        merged = sync._merge(local, remote)
        assert merged["next_id"] == 12

    def test_deep_copy_no_mutation(self, sync):
        """Merge must not mutate inputs."""
        local_task = self._task("Original")
        local = self._store(T1=local_task)
        remote = self._store(T1=self._task("Changed"))
        sync._merge(local, remote)
        assert local_task["title"] == "Original"
