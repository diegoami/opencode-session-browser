import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from opencode_session_browser.config import Config  # noqa: E402
from opencode_session_browser.discovery import Discovery  # noqa: E402
from opencode_session_browser.registry import Registry  # noqa: E402
from opencode_session_browser.source import SourceSpec  # noqa: E402
import fixtures  # noqa: E402


@pytest.fixture(autouse=True)
def no_exe_probe(monkeypatch):
    import opencode_session_browser.registry as r
    monkeypatch.setattr(r, "probe_version", lambda spec, cfg: {"version": "0.0.0-test", "note": "stub"})


@pytest.fixture
def std_root(tmp_path):
    root = tmp_path / "data" / "opencode"
    root.mkdir(parents=True)
    fixtures.make_standard(root / "opencode.db")
    return root


@pytest.fixture
def make_registry(tmp_path):
    made = []

    def make(specs, **cfgkw):
        cfg = Config(auto_discovery=False, **cfgkw)
        reg = Registry(cfg, cache_dir=tmp_path / "cache", specs=Discovery([], []))
        reg.add_specs(specs)
        reg.refresh_all(force=True)
        reg.index_once()
        made.append(reg)
        return reg

    yield make
    for r in made:
        r.stop()


def spec(id, root, env="custom", **kw):
    return SourceSpec(id=id, label=kw.pop("label", id), env=env, root=str(root), **kw)
