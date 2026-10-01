import json
import subprocess

import pytest

from opencode_session_browser import discovery, paths
from opencode_session_browser.config import Config, load_config, slug
from opencode_session_browser.registry import Registry
from opencode_session_browser.source import SourceSpec


def test_path_kind_and_basename():
    assert paths.path_kind("C:\\Users\\me\\proj") == "windows" and paths.path_kind("C:/Users/me") == "windows"
    assert paths.path_kind("\\\\wsl.localhost\\Ubuntu\\home\\x") == "windows"
    assert paths.path_kind("/home/me/proj") == "posix" and paths.path_kind("relative") == "unknown" and paths.path_kind(None) == "unknown"
    assert paths.basename_any("C:\\Users\\me\\proj\\") == "proj" and paths.basename_any("/home/me/proj/") == "proj"
    assert paths.basename_any("C:/a/b c/ünï") == "ünï" and paths.basename_any("/") == "/" and paths.basename_any("") == ""


def test_windows_to_wsl_and_unc():
    assert paths.windows_to_wsl("C:\\Users\\Me\\x") == "/mnt/c/Users/Me/x" and paths.windows_to_wsl("d:/data") == "/mnt/d/data"
    assert paths.windows_to_wsl("/home/x") is None
    assert paths.unc_wsl_parts("\\\\wsl.localhost\\Ubuntu-22.04\\home\\a b") == ("Ubuntu-22.04", "/home/a b")
    assert paths.unc_wsl_parts("//wsl$/Debian/root") == ("Debian", "/root") and paths.unc_wsl_parts("C:\\x") is None
    assert paths.wsl_to_unc("Ubuntu", "/home/x") == "\\\\wsl.localhost\\Ubuntu\\home\\x"


def test_compare_normalisation():
    assert paths.normalize_for_compare("C:\\Users\\Me\\") == paths.normalize_for_compare("c:/users/me")
    assert paths.normalize_for_compare("/Home/X/") != paths.normalize_for_compare("/home/x")


def test_local_equivalent_in_wsl(monkeypatch):
    monkeypatch.setattr(paths, "is_wsl", lambda: True); monkeypatch.setattr(paths, "is_windows", lambda: False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert paths.local_equivalent("C:\\Users\\a") == "/mnt/c/Users/a" and paths.local_equivalent("/home/a") == "/home/a"
    assert paths.local_equivalent("\\\\wsl.localhost\\Ubuntu\\home\\a") == "/home/a"
    assert paths.local_equivalent("\\\\wsl.localhost\\Other\\home\\a") is None
    assert paths.directory_exists("D:\\definitely\\not\\here\\xyz") is False


def test_resume_commands_per_environment(tmp_path, make_registry):
    reg = Registry(Config(auto_discovery=False), cache_dir=tmp_path)
    from opencode_session_browser.models import SessionSummary
    from opencode_session_browser.source import Source
    s = SessionSummary(id="ses_1", directory="C:/Users/me/it's", source_id="w")
    w = Source(SourceSpec("w", "Windows", "windows", "x"), tmp_path)
    cmd = reg.resume_commands(w, s)[0]
    assert cmd["command"] == "Set-Location -LiteralPath 'C:\\Users\\me\\it''s'; opencode -s ses_1"
    s2 = SessionSummary(id="ses_2", directory="/home/me/it's", source_id="u")
    u = Source(SourceSpec("u", "WSL: Ubuntu", "wsl", "x", distro="Ubuntu"), tmp_path)
    cmds = reg.resume_commands(u, s2)
    assert cmds[0]["command"] == "cd '/home/me/it'\\''s' && opencode -s ses_2"
    assert cmds[1]["command"].startswith("wsl.exe -d 'Ubuntu' --cd '/home/me/it''s' -- opencode -s ses_2")


def test_wsl_list_parsing_utf16(monkeypatch):
    out = "  NAME            STATE           VERSION\r\n* Ubuntu          Running         2\r\n  Debian          Stopped         2\r\n  docker-desktop  Stopped         2\r\n  Ubuntu 22.04    Running         2\r\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, ("\ufeff" + out).encode("utf-16-le"), b""))
    ds, err = discovery.list_wsl_distros()
    assert err is None and [(d.name, d.state, d.version, d.default) for d in ds] == [
        ("Ubuntu", "Running", "2", True), ("Debian", "Stopped", "2", False), ("docker-desktop", "Stopped", "2", False), ("Ubuntu 22.04", "Running", "2", False)]


def test_wsl_missing_is_graceful(monkeypatch):
    def boom(*a, **k): raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", boom)
    assert discovery.list_wsl_distros() == ([], "wsl.exe not found")


def test_windows_side_wsl_discovery(monkeypatch, tmp_path):
    """Windows-launched instance: enumerate distros and read their homes via the share (simulated with a local tree)."""
    from pathlib import Path
    share = tmp_path / "Ubuntu"
    for user in ("alice", "bob"):
        d = share / "home" / user / ".local" / "share" / "opencode"; d.mkdir(parents=True); (d / "opencode.db").write_bytes(b"")
    (share / "home" / "carol").mkdir()
    monkeypatch.setattr(discovery, "list_wsl_distros", lambda timeout=10: ([discovery.Distro("Ubuntu", "Running", "2", True), discovery.Distro("docker-desktop", "Stopped", "2")], None))
    real = discovery.Path
    monkeypatch.setattr(discovery, "Path", lambda p="": real(str(p).replace("\\\\wsl.localhost\\Ubuntu", str(share)).replace("\\", "/")) if "wsl" in str(p) else real(p))
    specs, distros, err = discovery.discover_wsl_from_windows(wake=True)
    assert sorted(s.label for s in specs) == ["WSL: Ubuntu (alice)", "WSL: Ubuntu (bob)"] and all(s.env == "wsl" and s.distro == "Ubuntu" for s in specs)
    assert distros[1].note and "system" in distros[1].note
    monkeypatch.setattr(discovery, "list_wsl_distros", lambda timeout=10: ([discovery.Distro("Ubuntu", "Stopped")], None))
    specs, distros, _ = discovery.discover_wsl_from_windows(wake=False)
    assert specs == [] and "not started" in distros[0].note


def test_windows_from_wsl_discovery(monkeypatch, tmp_path):
    mnt = tmp_path / "mnt" / "c" / "Users"
    for u in ("Zed", "Public", "Default"):
        (mnt / u).mkdir(parents=True)
    r = mnt / "Zed" / ".local" / "share" / "opencode"; r.mkdir(parents=True); (r / "opencode.db").write_bytes(b"")
    real = discovery.Path
    monkeypatch.setattr(discovery, "Path", lambda p="": real(str(p).replace("/mnt", str(tmp_path / "mnt"), 1) if str(p) == "/mnt" else p))
    specs = discovery.discover_windows_from_wsl()
    assert [(s.label, s.id, s.env) for s in specs] == [("Windows", "windows", "windows")] and "Zed" in specs[0].root


def test_no_hardcoded_identity(monkeypatch):
    import inspect
    src = inspect.getsource(discovery) + inspect.getsource(paths)
    import getpass
    from pathlib import Path
    for needle in {getpass.getuser(), Path.home().name} - {"root", ""}:
        assert needle.lower() not in src.lower()


def test_manual_sources_config(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"sources": [{"name": "Backup drive", "path": "D:\\old\\opencode", "kind": "sqlite", "status_url": "http://127.0.0.1:4096"}],
                               "wake_wsl": False, "poll_seconds": 9, "status_urls": {"x": "http://h"}}))
    c = load_config(cfg)
    assert c.sources[0].name == "Backup drive" and c.wake_wsl is False and c.poll_seconds == 9
    d = discovery.discover(Config(auto_discovery=False, sources=c.sources))
    assert d.specs[0].id == "backup-drive" and d.specs[0].env == "custom" and d.specs[0].status_url == "http://127.0.0.1:4096"
    d = discovery.discover(Config(auto_discovery=False, sources=c.sources + c.sources))
    assert [s.id for s in d.specs] == ["backup-drive", "backup-drive-2"]
    assert slug("WSL: Ubuntu 22.04") == "wsl-ubuntu-22-04"


def test_bad_config_is_a_clean_error(tmp_path):
    p = tmp_path / "c.json"; p.write_text("{oops")
    with pytest.raises(SystemExit):
        load_config(p)


def test_snapshot_decision():
    from opencode_session_browser import sqlite_access as sa
    assert sa.needs_snapshot("/tmp") is None or isinstance(sa.needs_snapshot("/tmp"), str)
