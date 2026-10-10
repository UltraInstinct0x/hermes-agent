"""Cron job output posted to Bot Chat as a visible assistant row that never wakes the agent
(``cron.bot_chat_delivery_mode: notice``). Kept out of ``hermes_state_messages`` (file-size cap) and bound
onto SessionDB through ``SessionMessagesMixin``; built on its _message_row_params/_execute_write primitives."""

from __future__ import annotations

import time
from typing import Any, Optional

from agent.message_metadata import MESSAGE_UID, stamp_message_uid
from hermes_state_common import CRON_NOTICE_DISPLAY_KIND
from hermes_state_detached_results import _LINEAGE_MATCH_SQL

_CRON_NOTICE_MATCH = "m.display_kind = ? AND json_extract(m.display_metadata, '$.delivery_id') = ?"


class SessionCronNoticeMixin:
    """Cron notices: ``role='assistant'``, ``display_kind='cron_notice'``, idempotent on ``delivery_id``."""

    def append_cron_notice(self, session_id: str, content: str, metadata: dict[str, Any],
                           message_uid: Optional[str] = None) -> tuple[int, bool]:
        """Post a cron job's output as a visible, non-user row that never wakes the agent.

        ``role='assistant'`` + ``display_kind='cron_notice'``; ``metadata`` (job id/name, run id, ...) rides in
        ``display_metadata``. ``metadata['delivery_id']`` is the identity: the check and the insert share the
        writer transaction and the lookup walks the compression lineage, so a retry, a second process or a
        post-rotation replay returns the existing row. Returns ``(row_id, inserted)``.
        """
        from hermes_state_messages import _INSERT_MESSAGE_SQL  # that module binds this mixin
        delivery_id = metadata.get("delivery_id")
        if not delivery_id:
            raise ValueError("Cron notice requires a stable delivery_id")
        msg = {"content": content, "display_kind": CRON_NOTICE_DISPLAY_KIND, "display_metadata": dict(metadata)}
        if message_uid:  # a live-session dict already stamped its uid; the row must carry the same one
            msg[MESSAGE_UID] = message_uid
        stamp_message_uid(msg)  # else a fresh occurrence: mint its durable id
        params = self._message_row_params(session_id, "assistant", msg, None, time.time(), keep_reasoning=True)

        def _do(conn):
            existing = conn.execute(_LINEAGE_MATCH_SQL + _CRON_NOTICE_MATCH + " LIMIT 1",
                                    (session_id, CRON_NOTICE_DISPLAY_KIND, delivery_id)).fetchone()
            if existing is not None:
                return existing[0], False
            self._check_transcript_write_guards(conn, session_id, None)  # an active turn lease is fine here
            msg_id = conn.execute(_INSERT_MESSAGE_SQL, params).lastrowid
            self._bump_session_counters(conn, session_id, 1, 0, unit=True)
            return msg_id, True

        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)
