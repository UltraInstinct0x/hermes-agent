"""Process-wide ceiling on LIVE delegated children (``delegation.max_total_children``).

``delegation.max_concurrent_children`` bounds one batch and the number of background calls, but nested
orchestrators bypass the async registry, so the real fan-out is M + M^2 per call and M * (M + M^2) across
background calls. This ledger counts children themselves, at every depth, per process, and refuses a batch
that would push the live count past the limit.

Accounting is lease based so the count cannot drift: a call takes one ``ChildLease`` holding one entry per task
index. Every release is by index into a set, so releasing twice is a no-op and the count can never go negative.
Entries leave the set when the child's run really ends (``release_when_settled``), when a child is known never to
have started (``release_unclaimed`` / ``release_unstarted``), or when ``_reconcile_locked`` finds the owner gone.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future
from typing import Iterable, Optional

logger = logging.getLogger("tools.delegate_tool")  # log-record parity with the delegation modules

# A timed-out child whose worker thread never unwinds is already unstoppable; stop charging it after this long.
_ABANDONED_WORKER_TTL_S = 900.0
# A dispatched unit gets this long to show up in the async registry before its unstarted children are written off.
_UNIT_REGISTRATION_GRACE_S = 30.0

_lock = threading.Lock()
_leases: set[ChildLease] = set()


class ChildBudgetExceeded(Exception):
    """A batch would push the live child count past ``delegation.max_total_children``."""

    def __init__(self, requested: int, live: int, limit: int):
        super().__init__(f"{requested} requested, {live} live, limit {limit}")
        self.requested, self.live, self.limit = requested, live, limit


def _unit_is_live(unit_id: str) -> bool:
    """Whether the async registry still owns this unit. Errs towards True: a wrong 'live' only delays a release."""
    try:
        from tools.async_delegation import is_delegation_live
        return is_delegation_live(unit_id)
    except Exception:
        return True


def _held_total_locked() -> int:
    return sum(len(lease._held) for lease in _leases)


def _is_orphan(lease: ChildLease, index: int, now: float, alive_threads: set[int]) -> bool:
    if index in lease._settling:
        return now - lease._settling[index] > _ABANDONED_WORKER_TTL_S
    if index in lease._started:
        return lease._started[index] not in alive_threads  # the thread running it is gone without releasing
    if index in lease._dispatched:
        return now - lease._created > _UNIT_REGISTRATION_GRACE_S and not _unit_is_live(lease._dispatched[index])
    return lease._owner not in alive_threads  # never started, never handed on, and its caller is gone


def _reconcile_locked() -> None:
    """Drop entries whose owner can no longer release them. Cheap: runs on admit and when the count is read."""
    if not _leases:
        return
    now = time.monotonic()
    alive_threads = {t.ident for t in threading.enumerate()}
    for lease in list(_leases):
        orphans = [i for i in lease._held if _is_orphan(lease, i, now, alive_threads)]
        if orphans:
            logger.warning("delegation child budget: reconciled %d orphaned child slot(s)", len(orphans))
            lease._held.difference_update(orphans)
        if not lease._held:
            _leases.discard(lease)


class ChildLease:
    """The slots one ``delegate_task`` call holds. Create through ``acquire_children`` only."""

    def __init__(self, count: int):
        self._held: set[int] = set(range(count))
        self._started: dict[int, Optional[int]] = {}  # task index -> id of the thread running it
        self._dispatched: dict[int, str] = {}  # task index -> async unit that owns it
        self._settling: dict[int, float] = {}  # task index -> when its timed-out worker was abandoned
        self._owner = threading.get_ident()
        self._created = time.monotonic()

    def held(self) -> int:
        with _lock:
            return len(self._held)

    def mark_started(self, index: int) -> None:
        with _lock:
            self._started[index] = threading.get_ident()

    def mark_dispatched(self, indexes: Iterable[int], unit_id: str) -> None:
        """The async registry now owns these children (its runner releases them)."""
        with _lock:
            for index in indexes:
                self._dispatched[index] = unit_id

    def release(self, index: int) -> None:
        with _lock:
            self._drop_locked([index])

    def release_when_settled(self, index: int, child: object) -> None:
        """Release now, unless a timed-out child's worker is still running: that thread is a live agent, so the
        slot is held until it unwinds (bounded by ``_ABANDONED_WORKER_TTL_S``)."""
        worker = getattr(child, "_abandoned_worker_future", None)
        if isinstance(worker, Future) and not worker.done():
            with _lock:
                self._settling[index] = time.monotonic()
            worker.add_done_callback(lambda _done: self.release(index))
            return
        self.release(index)

    def release_unclaimed(self) -> None:
        """End of the call: free every child that never started and was never handed to the async registry."""
        with _lock:
            self._drop_locked([i for i in self._held if i not in self._started and i not in self._dispatched])

    def release_unstarted(self, indexes: Iterable[int]) -> None:
        """End of an async unit's runner: free those of its children that never started."""
        with _lock:
            self._drop_locked([i for i in indexes if i in self._held and i not in self._started])

    def _drop_locked(self, indexes: Iterable[int]) -> None:
        self._held.difference_update(indexes)
        if not self._held:
            _leases.discard(self)


def acquire_children(count: int, limit: int) -> ChildLease:
    """Reserve ``count`` child slots or raise ``ChildBudgetExceeded``. Check and reserve share one lock hold, so
    concurrent calls cannot both pass the check."""
    with _lock:
        _reconcile_locked()
        live = _held_total_locked()
        if live + count > limit:
            raise ChildBudgetExceeded(count, live, limit)
        lease = ChildLease(count)
        _leases.add(lease)
        return lease


def live_children() -> int:
    """Children currently charged against the limit (process-wide)."""
    with _lock:
        _reconcile_locked()
        return _held_total_locked()


def _reset_for_tests() -> None:
    with _lock:
        _leases.clear()
