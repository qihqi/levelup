"""Public imports, packaged assets and the server launcher contract."""

import pytest

from levelup import Card, Game, RuleError, Rules
from levelup import cli
from levelup.assets import static_dir


def test_public_engine_exports_and_static_assets():
    assert Game(1).phase == "lobby"
    assert Card(0, "S", 2).rank == 2
    assert Rules(2, "S").suit(Card(0, "S", 2)) == "T"
    assert issubclass(RuleError, ValueError)
    assets = static_dir()
    assert 'id="hand"' in assets.joinpath("index.html").read_text(encoding="utf-8")
    assert "function renderTable" in assets.joinpath("app.js").read_text(encoding="utf-8")
    assert ".card" in assets.joinpath("style.css").read_text(encoding="utf-8")


def test_server_launcher_environment_and_overrides(monkeypatch):
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("LEVELUP_HOST", "127.0.0.1")
    monkeypatch.setenv("LEVELUP_PORT", "8768")
    monkeypatch.setattr("sys.argv", ["levelup-serve"])
    cli.main()
    assert calls[-1] == (("levelup.server:app",), {
        "host": "127.0.0.1", "port": 8768, "workers": 1, "ws_max_size": 16384})
    monkeypatch.setattr("sys.argv", ["levelup-serve", "--host", "0.0.0.0", "--port", "9000"])
    cli.main()
    assert calls[-1][1]["host"] == "0.0.0.0" and calls[-1][1]["port"] == 9000
    monkeypatch.setattr("sys.argv", ["levelup-serve", "--port", "65536"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
