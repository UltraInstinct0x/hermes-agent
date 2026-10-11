"""``profiles.list`` carries the Bot Mode title the core already reads, and the contract declares the field."""

from __future__ import annotations

from hermes_cli.profiles import ProfileInfo
from tui_gateway.contracts.profiles_vault_complete_foreign_subagents import ProfileRow


def test_profiles_list_rows_carry_bot_title(tmp_path, monkeypatch):
    from tui_gateway import server

    titled = ProfileInfo(name="skynet", path=tmp_path / "skynet", is_default=False, gateway_running=False,
                         bot_title="Skynet")
    untitled = ProfileInfo(name="plain", path=tmp_path / "plain", is_default=False, gateway_running=False)
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda **_: [titled, untitled])

    reply = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "profiles.list",
                                   "params": {"include_sessions": False}})

    rows = {row["name"]: row for row in reply["result"]["profiles"]}
    assert rows["skynet"]["bot_title"] == "Skynet"
    assert rows["plain"]["bot_title"] == ""


def test_profile_row_contract_declares_bot_title_as_optional_nullable():
    assert "bot_title" in ProfileRow.model_fields
    assert ProfileRow(name="x", path="/x").bot_title is None
    assert ProfileRow(name="x", path="/x", bot_title=None).bot_title is None
