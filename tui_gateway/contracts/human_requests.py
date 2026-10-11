"""The human-request ledger (``human_requests.py`` / ``methods_human_requests.py``): questions the agent put
to a person (approvals, clarifications, sudo and secret prompts), who answered them, and how they ended.

``request.resolved`` is the live half: it tells every client attached to the session that a request was
answered, by whom, and with what, next to the older ``request.cancel`` (which stays for clients that predate it).
``request.list`` and ``request.history`` are the durable half, read from the profile's ``state.db``.
The secret kinds (``sudo``, ``secret``) carry no prompt, choice or answer text anywhere on this surface.
"""

from __future__ import annotations

from pydantic import Field

from .base import JsonValue, Payload, Result, WireEnum
from .common import ProfileParams
from .registry import event, method


class HumanRequestKind(WireEnum):
    approval = "approval"
    clarify = "clarify"
    sudo = "sudo"
    secret = "secret"
    other = "other"


class HumanRequestState(WireEnum):
    open = "open"
    answered = "answered"
    expired = "expired"
    withdrawn = "withdrawn"
    cancelled = "cancelled"
    lost_on_restart = "lost_on_restart"  # the gateway restarted while it was open; the bot will ask again


class AnsweredBy(Result):
    """Who answered. ``client_id`` is ``unknown`` for a client that never identified itself, and for an answer given
    on a surface that does not reach this gateway (Telegram, CLI)."""

    client_id: str
    label: str | None = None
    kind: str | None = None


class HumanRequestRow(Result):
    """One ledger row. ``session_id`` is the stored (durable) session id; the request id is what ``request.answer``
    takes. ``answers`` is clarify only (newest lock per question); sudo and secret rows have no prompt, choices or
    answers."""

    request_id: str
    kind: HumanRequestKind
    profile: str
    bot_key: str | None = None  # gatewayId::profileId; the gateway cannot know it, a client may fill it
    session_id: str | None = None
    session_key: str | None = None
    call_id: str | None = None
    prompt: str | None = None
    choices: list[JsonValue] | None = None  # approval choices, or the clarify questions as sent
    state: HumanRequestState
    outcome: str | None = None  # once | session | always | deny | submitted | cancelled | skipped | timed_out
    cancel_reason: str | None = None
    created_at: float
    expires_at: float | None = None
    answered_at: float | None = None
    answered_by: AnsweredBy | None = None
    answered_via: str | None = None  # frame | approval.respond | clarify.lock | request.answer | external
    answers: dict[str, str | None] | None = None


class RequestListParams(ProfileParams):
    answered_limit: int | None = None  # how many recently closed requests to include (default 20, 0 = none)


class RequestListResult(Result):
    open: list[HumanRequestRow] = Field(default_factory=list)
    recent: list[HumanRequestRow] = Field(default_factory=list)
    open_count: int


method("request.list", params=RequestListParams, result=RequestListResult,
       doc="Every request a live gateway still waits on across ALL sessions of the profile, plus the most recent "
           "closed ones. open_count is what a restart guard asks before restarting.")


class RequestHistoryParams(ProfileParams):
    session_id: str | None = None  # stored session id
    kind: HumanRequestKind | None = None
    states: list[HumanRequestState] | None = None
    limit: int | None = None  # default 50, at most 200
    before: float | None = None  # created_at cursor: the previous page's next_before


class RequestHistoryResult(Result):
    requests: list[HumanRequestRow] = Field(default_factory=list)
    next_before: float | None = None


method("request.history", params=RequestHistoryParams, result=RequestHistoryResult,
       doc="Newest-first page of the human-request ledger (retention 90 days).")


class RequestResolvedPayload(Payload):
    """Emitted when a request was answered (by any client, or on another surface), to the clients attached to
    the session, answerer included (the id lets a client dedupe). ``choice`` is the approval decision, ``answers``
    the clarify answers (clipped); both are null for sudo and secret."""

    id: str
    method: str
    kind: HumanRequestKind
    session_id: str
    outcome: str | None = None
    choice: str | None = None
    answers: dict[str, str | None] | None = None
    answered_by: AnsweredBy
    at: float


event("request.resolved", RequestResolvedPayload,
      doc="A server→client request was answered; says who and with what. request.cancel (reason resolved) still "
          "follows for approvals answered over RPC.")
