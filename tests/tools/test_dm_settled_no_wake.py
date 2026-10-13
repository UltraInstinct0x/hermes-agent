"""A settled bot-to-bot delivery must not wake the sender a second time."""
import json

from tools.process_registry import is_settled_dm_delivery

COMMAND = "/usr/bin/python3 tools/bot_mode_dm.py --run-delivery --author '{}' query-file /tmp/dm.txt hermes -p scout chat -Q"


def evt(output, exit_code=0, command=COMMAND):
    return {"type": "completion", "session_id": "proc_x", "command": command, "exit_code": exit_code, "output": output}


def test_a_settled_status_with_no_reply_is_skipped():
    assert is_settled_dm_delivery(evt(json.dumps({"status": "settled"})))
    assert is_settled_dm_delivery(evt('{"status": "queued", "reply": ""}'))


def test_a_reply_wakes_the_sender():
    assert not is_settled_dm_delivery(evt(json.dumps({"status": "settled", "reply": "Mango."})))
    assert not is_settled_dm_delivery(evt("Reply from Scout:\nMango."))


def test_a_failure_wakes_the_sender():
    assert not is_settled_dm_delivery(evt(json.dumps({"status": "settled"}), exit_code=1))
    assert not is_settled_dm_delivery(evt(json.dumps({"error": "Delivery failed", "reason": "target_busy"})))
    assert not is_settled_dm_delivery(evt(json.dumps({"status": "settled", "error": "x"})))


def test_anything_that_is_not_a_dm_runner_or_not_readable_wakes_the_sender():
    assert not is_settled_dm_delivery(evt(json.dumps({"status": "settled"}), command="npm run build"))
    assert not is_settled_dm_delivery(evt("not json at all"))
    assert not is_settled_dm_delivery(evt("noise {\"status\": \"settled\"}"))
    assert not is_settled_dm_delivery(evt(""))
