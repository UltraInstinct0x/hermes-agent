"""The Mac screen server child must see the parent's third-party packages, and a missing one is named."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.bot_desktop.macos import preflight, runtime as mac

REPO = Path(__file__).resolve().parents[2]

# What the launchd-style gateway does: isolated base interpreter, venv site-packages added in-process.
_LAUNCHER = """
import json, sys
sys.path.insert(0, {repo!r})
sys.path.append({site!r})
from tools.bot_desktop.macos import preflight
env = {{"PATH": "/usr/bin:/bin"}}
{env_line}
print(json.dumps(preflight.missing_in_child(env, cwd={repo!r}, modules=["PIL"])))
"""


def _fake_site(tmp_path: Path) -> Path:
    site = tmp_path / "venv" / "lib" / "site-packages"
    (site / "PIL").mkdir(parents=True)
    (site / "PIL" / "__init__.py").write_text("", encoding="utf-8")
    return site


def _base_python() -> str:
    """The base interpreter (what the gateway launcher runs), not a venv python that finds its own packages."""
    base = getattr(sys, "_base_executable", sys.executable)
    if subprocess.run([base, "-I", "-c", "import PIL"], capture_output=True).returncode == 0:
        pytest.skip("the base interpreter already has Pillow, so the isolation cannot be shown")
    return base


def _run_isolated(tmp_path: Path, env_line: str) -> list[str]:
    base = _base_python()
    code = _LAUNCHER.format(repo=str(REPO), site=str(_fake_site(tmp_path)), env_line=env_line)
    out = subprocess.run([base, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_plain_child_of_an_isolated_launcher_cannot_import_venv_packages(tmp_path):
    # The bug: no PYTHONPATH handed down, so the venv-only package is invisible to the child.
    assert _run_isolated(tmp_path, "") == ["PIL"]


def test_the_child_env_built_from_the_parent_path_resolves_venv_packages(tmp_path):
    assert _run_isolated(tmp_path, "env = preflight.with_parent_site_packages(env)") == []


def test_only_site_packages_dirs_are_forwarded_and_nothing_else_is_added(tmp_path):
    site = _fake_site(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    paths = [str(plain), str(site), str(site), "", "/nope/site-packages"]
    env = preflight.with_parent_site_packages({"PATH": "/bin", "HOME": "/h"}, paths)
    assert env == {"PATH": "/bin", "HOME": "/h", "PYTHONPATH": str(site)}


def test_an_existing_pythonpath_is_kept_after_the_parent_entries(tmp_path):
    site = _fake_site(tmp_path)
    env = preflight.with_parent_site_packages({"PYTHONPATH": "/x"}, [str(site)])
    assert env["PYTHONPATH"].split(":") == [str(site), "/x"]


def test_server_env_gains_pythonpath_but_no_secrets(monkeypatch):
    import tools.environments.local as local
    monkeypatch.setattr(local, "served_profile_child_env", lambda **kw: {"PATH": "/bin"})
    monkeypatch.setattr(mac, "_cfg", lambda: {})
    monkeypatch.setattr(mac._rt, "_profile_name", lambda: "default")
    monkeypatch.setattr(preflight, "site_packages_entries", lambda paths=None: ["/venv/site-packages"])
    env = mac._server_env()
    assert env["PYTHONPATH"] == "/venv/site-packages"
    assert set(env) <= {"PATH", "HOME", "HERMES_HOME", "HERMES_BD_PROFILE", "PYTHONPATH"}


def test_the_required_list_covers_every_third_party_import_in_the_macos_package():
    own = {"tools", "hermes_constants", "hermes_cli", "agent"}
    found = set()
    for path in (REPO / "tools" / "bot_desktop" / "macos").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    third_party = {n for n in found if n not in sys.stdlib_module_names and n not in own}
    assert third_party <= set(preflight.REQUIRED_MODULES)


def test_a_missing_package_is_named_by_its_install_name():
    assert preflight.missing_package_message(["PIL"]) == "A Python package the Mac screen needs is missing: Pillow"
    assert "—" not in preflight.missing_package_message(["PIL", "other"])


def test_status_blocker_names_the_missing_package(monkeypatch):
    monkeypatch.setattr(mac, "find_cua_driver", lambda: "/bin/cua-driver")
    monkeypatch.setattr(mac, "_permissions", lambda cmd: {"accessibility": True, "screen_recording": True})
    monkeypatch.setattr(mac, "_modules_missing_for_server", lambda: ["PIL"])
    p = mac.probe(force=True)
    assert p.supported is False and p.installed is True
    assert p.reason == "A Python package the Mac screen needs is missing: Pillow."


def test_log_tail_summary_is_the_last_line_without_secrets(tmp_path):
    log = tmp_path / "launcher.log"
    log.write_text("Traceback (most recent call last):\n  File \"x.py\", line 31, in <module>\n"
                   "ModuleNotFoundError: No module named 'PIL'\n\n", encoding="utf-8")
    assert preflight.log_tail_summary(log) == "ModuleNotFoundError: No module named 'PIL'"
    log.write_text("boom OPENAI_API_KEY=sk-abcdef123456789 end", encoding="utf-8")
    assert "sk-abcdef" not in preflight.log_tail_summary(log)
    log.write_text("x" * 500, encoding="utf-8")
    assert len(preflight.log_tail_summary(log)) == 200
    assert preflight.log_tail_summary(tmp_path / "missing.log") is None


def test_the_ended_before_start_error_carries_the_log_summary(tmp_path):
    (tmp_path / "launcher.log").write_text("ModuleNotFoundError: No module named 'PIL'\n", encoding="utf-8")
    assert mac._why_it_ended(tmp_path) == (
        "The Mac screen session ended before it started (ModuleNotFoundError: No module named 'PIL'). "
        "See launcher.log for details.")


def test_the_ended_before_start_error_without_a_log_is_unchanged(tmp_path):
    assert mac._why_it_ended(tmp_path) == (
        "The Mac screen session ended before it started. See launcher.log for details.")
