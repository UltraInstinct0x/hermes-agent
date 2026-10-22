"""cron notice -> phone push hook (``cron.bot_chat_push``)."""
import json
from pathlib import Path

from cron import bot_chat_notice
from cron import bot_chat_push as push

SECRET = "s" * 40


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _profile(tmp_path: Path, name: str = "appdev") -> Path:
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True)
    return home


def test_secret_found_under_the_profile_the_bff_serves(tmp_path):
    home = _profile(tmp_path, "appdev")
    served = _profile(tmp_path, "hermesbot") / "runtime" / "bff"
    served.mkdir(parents=True)
    (served / "push-intake-secret").write_text(SECRET)
    assert push.read_secret(home, env={}) == SECRET


def test_sends_message_proactive_with_secret_header(tmp_path):
    home = _profile(tmp_path)
    (home / "runtime" / "bff").mkdir(parents=True)
    (home / "runtime" / "bff" / "push-intake-secret").write_text(SECRET)
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["body"], seen["secret"] = req.full_url, json.loads(req.data), req.get_header("X-hermes-push-secret")
        return _Resp()

    assert push.notify_phone(home, "cron-k1", "done\n  ok", env={}, opener=opener) == ("sent", "")
    assert seen["url"] == push.DEFAULT_URL
    assert seen["secret"] == SECRET
    assert seen["body"] == {"type": "message.proactive", "profile": "appdev", "messageId": "cron-k1", "text": "done ok"}


def test_skips_without_secret_and_for_non_loopback(tmp_path):
    home = _profile(tmp_path)
    assert push.notify_phone(home, "k", "t", env={})[0] == "skipped"
    env = {"HERMES_BOT_PUSH_URL": "http://example.com/x"}
    assert push.notify_phone(home, "k", "t", env=env) == ("skipped", "intake url is not loopback")


def test_failure_never_raises(tmp_path):
    home = _profile(tmp_path)
    def boom(req, timeout):
        raise ConnectionRefusedError()
    assert push.notify_phone(home, "k", "t", env={"HERMES_BOT_PUSH_SECRET_FILE": str(tmp_path / "x")}, opener=boom)[0] in ("skipped", "failed")
    (tmp_path / "x").write_text(SECRET)
    assert push.notify_phone(home, "k", "t", env={"HERMES_BOT_PUSH_SECRET_FILE": str(tmp_path / "x")}, opener=boom) == ("failed", "ConnectionRefusedError")


def _deliver(monkeypatch, tmp_path, wrote, for_failure=False):
    calls = []
    monkeypatch.setattr(bot_chat_notice, "post_bot_chat_notice", lambda home, text, notice: wrote)
    monkeypatch.setattr(push, "notify_phone", lambda home, mid, text: calls.append((mid, text)) or ("sent", ""))
    home = _profile(tmp_path)
    err = bot_chat_notice.deliver_bot_chat_notice({"id": "j1", "name": "sync"}, "result text", home, "key1", "bot-chat:appdev",
                                                  for_failure=for_failure, suppressed=False)
    assert err is None
    return calls


def test_new_result_notice_pushes_once(monkeypatch, tmp_path):
    assert _deliver(monkeypatch, tmp_path, wrote=True) == [("cron-key1", "result text")]


def test_existing_row_and_failure_do_not_push(monkeypatch, tmp_path):
    assert _deliver(monkeypatch, tmp_path, wrote=False) == []
    assert _deliver(monkeypatch, tmp_path / "b", wrote=True, for_failure=True) == []


def test_message_id_fits_the_bff_id_rule():
    assert push.build_event("p", "cron-a/b c" + "x" * 100, "t")["messageId"] == ("cron-abc" + "x" * 100)[:80]
