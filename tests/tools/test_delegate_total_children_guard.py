"""delegation.max_total_children: a process-wide ceiling on live delegated children, at every depth.

Three layers: the ledger itself (leases never leak or go negative), the config getter (default derivation and
validation), and real ``delegate_task`` calls against the scripted model server (admission, refusal, and the
counter returning to zero after completion, a child exception, a timeout and ``stop``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.tools.stub_openai_server import Reply, StubServer
from tools import async_delegation, delegate_tool, delegate_tool_config
from tools import delegation_child_budget as budget
from tools.delegate_tool import delegate_task, list_active_subagents
from tools.delegate_tool_dispatch import _Batch, _dispatch_background


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(delegate_tool_config, "_TOTAL_CHILDREN_WARNED", False)
    budget._reset_for_tests()
    yield
    assert budget.live_children() == 0, "a test left children charged against the budget"
    budget._reset_for_tests()


def _cfg(**delegation):
    return patch("tools.delegate_tool._load_config", return_value=dict(delegation))


# --- config -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("delegation, expected", [
    ({"max_concurrent_children": 8}, 16),  # flat (max_spawn_depth 1): 2*M
    ({"max_concurrent_children": 8, "max_spawn_depth": 2}, 72),  # nested: M + M*M, one full depth-2 call
    ({"max_concurrent_children": 8, "max_spawn_depth": 3}, 72),
    ({"max_concurrent_children": 8, "max_spawn_depth": 2, "orchestrator_enabled": False}, 16),  # no nesting possible
    ({}, 20),  # M defaults to 10
    ({"max_concurrent_children": 8, "max_spawn_depth": 2, "max_total_children": 30}, 30),
    ({"max_concurrent_children": 8, "max_total_children": "5"}, 5),
])
def test_default_derivation_and_explicit_value(delegation, expected):
    with _cfg(**delegation):
        assert delegate_tool._get_max_total_children() == expected


@pytest.mark.parametrize("bad", ["many", 0, -3, True, 2.5, 10**6, [4]])
def test_invalid_value_uses_the_default_and_warns_once(bad, caplog):
    with _cfg(max_concurrent_children=8, max_spawn_depth=2, max_total_children=bad):
        with caplog.at_level(logging.WARNING, logger="tools.delegate_tool"):
            assert delegate_tool._get_max_total_children() == 72
            assert delegate_tool._get_max_total_children() == 72
    warnings = [r for r in caplog.records if "max_total_children" in r.getMessage()]
    assert len(warnings) == 1


# --- ledger -----------------------------------------------------------------------------------------------------

def test_admits_up_to_the_limit_and_refuses_beyond_without_changing_the_count():
    first = budget.acquire_children(3, 5)
    assert budget.live_children() == 3
    with pytest.raises(budget.ChildBudgetExceeded) as refused:
        budget.acquire_children(3, 5)
    assert (refused.value.requested, refused.value.live, refused.value.limit) == (3, 3, 5)
    assert budget.live_children() == 3  # a refused batch charges nothing
    second = budget.acquire_children(2, 5)
    assert budget.live_children() == 5
    first.release_unclaimed()
    second.release_unclaimed()
    assert budget.live_children() == 0


def test_release_is_idempotent_and_the_count_never_goes_negative():
    lease = budget.acquire_children(2, 10)
    other = budget.acquire_children(1, 10)
    for _ in range(3):
        lease.release(0)
        lease.release(5)  # an index it never held
    assert budget.live_children() == 2
    lease.release(1)
    lease.release_unclaimed()
    lease.release_unstarted([0, 1])
    assert budget.live_children() == 1
    other.release(0)
    assert budget.live_children() == 0


def test_release_unclaimed_spares_started_and_dispatched_children():
    lease = budget.acquire_children(4, 10)
    lease.mark_started(0)
    lease.mark_dispatched([1], "deleg_x")
    lease.release_unclaimed()
    assert lease.held() == 2  # 0 releases itself when its run ends, 1 when its unit's runner ends
    lease.release_unstarted([1, 2, 3])
    assert lease.held() == 1
    lease.release(0)
    assert budget.live_children() == 0


def test_concurrent_admissions_never_pass_the_limit_together():
    admitted, refused = [], []
    done = threading.Event()  # callers stay alive while holding their lease, as a running tool call does

    def ask():
        try:
            admitted.append(budget.acquire_children(2, 10))
        except budget.ChildBudgetExceeded:
            refused.append(1)
        done.wait(timeout=30)

    threads = [threading.Thread(target=ask) for _ in range(20)]
    [t.start() for t in threads]
    deadline = time.monotonic() + 15
    while len(admitted) + len(refused) < 20 and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert len(admitted) == 5 and len(refused) == 15
        assert budget.live_children() == 10
    finally:
        done.set()
        [t.join() for t in threads]
        for lease in admitted:
            lease.release_unclaimed()


def test_a_timed_out_worker_stays_charged_until_it_unwinds():
    lease = budget.acquire_children(1, 10)
    worker = Future()
    worker.set_running_or_notify_cancel()
    lease.mark_started(0)
    lease.release_when_settled(0, SimpleNamespace(_abandoned_worker_future=worker))
    assert budget.live_children() == 1
    worker.set_result(None)
    assert budget.live_children() == 0


def test_reconcile_drops_slots_whose_owner_is_gone(monkeypatch):
    dead_thread_ident = -1
    lease = budget.acquire_children(4, 10)
    lease._owner = dead_thread_ident  # never started, caller gone
    lease._started[1] = dead_thread_ident  # started, thread gone without releasing
    lease._dispatched[2] = "deleg_gone"  # its async unit no longer exists
    lease._settling[3] = time.monotonic() - budget._ABANDONED_WORKER_TTL_S - 1  # abandoned worker, long stuck
    lease._created -= budget._UNIT_REGISTRATION_GRACE_S + 1
    lease._held -= {0}  # index 0 was released normally
    assert budget.live_children() == 0


def test_reconcile_keeps_slots_that_are_really_running():
    lease = budget.acquire_children(3, 10)
    lease.mark_started(0)  # this thread is alive
    lease._dispatched[1] = "deleg_live"
    lease._created -= budget._UNIT_REGISTRATION_GRACE_S + 1
    lease._settling[2] = time.monotonic()
    with patch("tools.delegation_child_budget._unit_is_live", return_value=True):
        assert budget.live_children() == 3
    lease.release(0), lease.release(1), lease.release(2)


# --- real delegate_task against the scripted model server ------------------------------------------------------

def _server(script=None):
    return StubServer(script or (lambda index, messages: Reply(text="child done")))


def _parent(server, depth=0):
    from run_agent import AIAgent
    agent = AIAgent(api_key="stub", base_url=server.base_url, provider="openrouter", api_mode="chat_completions",
                    model="stub-model", platform="cli", quiet_mode=True, skip_context_files=True, skip_memory=True,
                    save_trajectories=False, enabled_toolsets=["file", "delegation"], session_id=f"parent-{depth}")
    agent._delegate_depth = depth
    return agent


def _tasks(n):
    return [{"goal": f"Summarize the contents of module number {i} of the sample project"} for i in range(n)]


def test_call_below_the_cap_runs_and_the_counter_returns_to_zero():
    seen_live: list[int] = []
    server = _server(lambda index, messages: Reply(text="child done", on_request=lambda: seen_live.append(budget.live_children())))
    parent = _parent(server)
    try:
        with _cfg(max_concurrent_children=4, max_total_children=3):
            raw = delegate_task(tasks=_tasks(2), parent_agent=parent, background=False)
        assert "results" in json.loads(raw), raw
        results = json.loads(raw)["results"]
        assert [r["status"] for r in results] == ["completed", "completed"]
        assert seen_live and max(seen_live) == 2  # both children were charged while running
        assert budget.live_children() == 0
    finally:
        parent.close()
        server.close()


def test_sync_call_over_the_cap_is_refused_with_a_calm_error_and_the_counter_is_untouched():
    server = _server()
    parent = _parent(server, depth=1)  # a nested orchestrator's call is always synchronous
    held = budget.acquire_children(2, 99)  # siblings elsewhere in the tree
    try:
        with _cfg(max_concurrent_children=4, max_spawn_depth=3, max_total_children=3):
            refused = json.loads(delegate_task(tasks=_tasks(2), parent_agent=parent, background=False))
            assert "inline" in refused["error"] and "2 of 3" in refused["error"]
            assert "—" not in refused["error"]
            assert server.requests == []  # nothing was spawned
            assert budget.live_children() == 2  # unchanged by the refusal
            # The parent carries on: one task still fits in the remaining slot.
            carried_on = json.loads(delegate_task(tasks=_tasks(1), parent_agent=parent, background=False))
        assert carried_on["results"][0]["status"] == "completed"
        assert budget.live_children() == 2
    finally:
        held.release_unclaimed()
        parent.close()
        server.close()


def test_background_call_over_the_cap_is_refused_not_dispatched():
    server = _server()
    parent = _parent(server)
    held = budget.acquire_children(14, 99)
    async_delegation._reset_for_tests()
    try:
        with _cfg(max_concurrent_children=8, max_total_children=16):
            refused = json.loads(delegate_task(tasks=_tasks(3), parent_agent=parent, background=True))
        assert "Wait for running subagents" in refused["error"] and "14 of 16" in refused["error"]
        assert server.requests == [] and async_delegation.active_count() == 0
        assert budget.live_children() == 14
    finally:
        held.release_unclaimed()
        parent.close()
        server.close()


def test_a_child_exception_releases_its_slot(monkeypatch):
    from run_agent import AIAgent
    real = AIAgent.run_conversation

    def exploding(self, *args, **kwargs):
        if getattr(self, "_delegate_depth", 0) > 0:
            raise RuntimeError("child blew up")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(AIAgent, "run_conversation", exploding)
    server = _server()
    parent = _parent(server)
    try:
        with _cfg(max_concurrent_children=4, max_total_children=4):
            results = json.loads(delegate_task(tasks=_tasks(2), parent_agent=parent, background=False))["results"]
        assert [r["status"] for r in results] == ["error", "error"]
        assert budget.live_children() == 0
    finally:
        parent.close()
        server.close()


def test_an_exception_escaping_the_child_runner_still_releases_every_slot(monkeypatch):
    server = _server()
    parent = _parent(server)

    def raising(*_args, **_kwargs):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(delegate_tool, "_run_single_child", raising)
    try:
        with _cfg(max_concurrent_children=4, max_total_children=4):
            results = json.loads(delegate_task(tasks=_tasks(3), parent_agent=parent, background=False))["results"]
        assert {r["status"] for r in results} == {"error"}
        assert budget.live_children() == 0
    finally:
        parent.close()
        server.close()


def test_a_child_construction_failure_releases_the_whole_reservation(monkeypatch):
    server = _server()
    parent = _parent(server)
    monkeypatch.setattr(delegate_tool, "_build_children", lambda *a, **k: ([], "no credentials"))
    try:
        with _cfg(max_concurrent_children=4, max_total_children=4):
            assert "no credentials" in json.loads(delegate_task(tasks=_tasks(3), parent_agent=parent, background=False))["error"]
        assert budget.live_children() == 0
    finally:
        parent.close()
        server.close()


def test_stop_releases_the_slot():
    release = threading.Event()
    server = _server(lambda index, messages: Reply(text="never needed", delay=8.0) if not release.is_set() else Reply(text="late"))
    parent = _parent(server)
    outcome: dict = {}

    def run():
        with _cfg(max_concurrent_children=4, max_total_children=4):
            outcome["raw"] = delegate_task(tasks=_tasks(1), parent_agent=parent, background=False)

    worker = threading.Thread(target=run, daemon=True)
    try:
        with _cfg(max_concurrent_children=4, max_total_children=4):
            worker.start()
            deadline = time.monotonic() + 20
            while not server.requests and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.requests and budget.live_children() == 1
            sid = list_active_subagents()[0]["subagent_id"]
            stopped = json.loads(delegate_task(action="stop", subagent_id=sid, parent_agent=parent))
            assert stopped["status"] == "interrupt_requested"
            worker.join(timeout=40)
        assert not worker.is_alive()
        assert budget.live_children() == 0
    finally:
        release.set()
        parent.close()
        server.close()


def test_list_reports_the_live_count_and_limit():
    server = _server()
    parent = _parent(server)
    held = budget.acquire_children(3, 99)
    try:
        with _cfg(max_concurrent_children=8, max_spawn_depth=2):
            payload = json.loads(delegate_task(action="list", parent_agent=parent))
        assert payload["child_budget"] == {"live": 3, "limit": 72}
    finally:
        held.release_unclaimed()
        parent.close()
        server.close()


def test_schema_text_states_the_real_limits():
    with _cfg(max_concurrent_children=8, max_spawn_depth=2):
        overrides = delegate_tool._build_dynamic_schema_overrides()
    assert "At most 72 subagents" in overrides["parameters"]["properties"]["tasks"]["description"]
    assert "max_total_children limit (72)" in overrides["description"]


# --- timeout and the async fallback, with controllable children ---------------------------------------------------

class _SlowChild:
    """A child whose worker outlives the timeout until the test lets it finish."""

    def __init__(self):
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.model = "test-model"
        self.session_prompt_tokens = self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.started, self.allow_finish = threading.Event(), threading.Event()

    def run_conversation(self, **_kwargs):
        self.started.set()
        assert self.allow_finish.wait(timeout=30)
        return {"final_response": "", "completed": False, "interrupted": True, "api_calls": 1, "messages": []}

    def hard_interrupt(self, _reason=None):
        pass

    def get_activity_summary(self):
        return {"api_call_count": 1}

    def close(self):
        pass


def _batch_of(children, lease):
    parent = SimpleNamespace(session_id="p", _current_task_id=None, _active_children=list(children),
                             _active_children_lock=threading.Lock(), _delegate_depth=0)
    tasks = [{"goal": f"g{i}"} for i in range(len(children))]
    return _Batch(
        task_list=tasks, children=[(i, tasks[i], c) for i, c in enumerate(children)], parent_agent=parent,
        creds={"model": "test-model"}, context=None, top_role="leaf", max_children=len(children), live_deleg_id=None,
        live_writers=[], live_paths=[], origin_wake_sid="", origin_ui_session_id="", origin_owner_transport=None,
        origin_owner_session_record=None, origin_session_history_delivery=False, overall_start=time.monotonic(),
        lease=lease,
    )


def test_timeout_keeps_the_slot_until_the_abandoned_worker_unwinds(monkeypatch):
    child = _SlowChild()
    lease = budget.acquire_children(1, 5)
    batch = _batch_of([child], lease)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.5)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    try:
        entry = batch.run_child(0, batch.task_list[0], child)
        assert entry["status"] == "timeout"
        assert budget.live_children() == 1  # the worker thread is still a live agent
    finally:
        child.allow_finish.set()
    deadline = time.monotonic() + 15
    while budget.live_children() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert budget.live_children() == 0


@pytest.fixture
def async_registry(monkeypatch):
    import queue
    from tools.process_registry import process_registry
    async_delegation._reset_for_tests()
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    yield
    async_delegation._reset_for_tests()


def test_full_async_pool_falls_back_to_sync_with_the_note_and_the_run_is_counted(async_registry, monkeypatch):
    child = _SlowChild()
    lease = budget.acquire_children(1, 5)
    batch = _batch_of([child], lease)
    child.allow_finish.set()
    live_while_running: list[int] = []
    original = child.run_conversation
    child.run_conversation = lambda **kw: (live_while_running.append(budget.live_children()), original(**kw))[1]
    # Another background call already holds the single async slot.
    with async_delegation._records_lock:
        async_delegation._records["deleg_busy"] = {
            "delegation_id": "deleg_busy", "slot_key": "deleg_busy", "status": "running", "goals": ["x"]}
    monkeypatch.setattr(delegate_tool, "_get_max_async_children", lambda: 1)
    monkeypatch.setattr("tools.delegate_tool_dispatch._resolve_async_wake_sid", lambda *a, **k: "")
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    try:
        result = json.loads(_dispatch_background(batch))
        assert "at capacity" in result["note"] and "max_concurrent_children" in result["note"]
        assert live_while_running == [1]
        assert lease.held() == 0
    finally:
        with async_delegation._records_lock:
            async_delegation._records.pop("deleg_busy", None)
        lease.release_unclaimed()


def test_async_unit_that_never_starts_is_released_by_its_runner(async_registry, monkeypatch):
    child = _SlowChild()
    lease = budget.acquire_children(1, 5)
    batch = _batch_of([child], lease)
    monkeypatch.setattr("tools.delegate_tool_dispatch._resolve_async_wake_sid", lambda *a, **k: "")

    def pool_failure(*_args, **_kwargs):
        raise RuntimeError("could not start workers")

    monkeypatch.setattr("tools.delegate_tool_dispatch._execute_and_aggregate", pool_failure)
    try:
        _dispatch_background(batch)
        deadline = time.monotonic() + 15
        while lease.held() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert lease.held() == 0 and budget.live_children() == 0
    finally:
        lease.release_unclaimed()
