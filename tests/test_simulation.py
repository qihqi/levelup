import json
from pathlib import Path
import subprocess
import sys

import pytest

from levelup.game import Game, RuleError
from levelup.simulation import Simulation

BASIC = ("basic",) * 4


def test_seeded_round_trace_replays_every_command(tmp_path):
    path = tmp_path / "runs.jsonl"
    with Simulation(seed=42, strategies=BASIC, log_path=path, trace=True) as sim:
        result = sim.run_round()
        assert sim.game.phase == "round_end"
        assert all(not hand for hand in sim.game.hands)
        assert sum(len(p["cards"]) for t in sim.game.history for p in t["plays"]) == 100
        assert len(sim.game.bottom) == 8
        assert sim.observation(0).history
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert events[0]["event"] == "run_start" and events[-1]["event"] == "run_end"
    assert len({e["run_id"] for e in events}) == 1
    commands = [e for e in events if e["event"] == "command"]
    assert any(e["action"] == "draw" for e in commands)
    assert any(e["action"] == "bid" for e in commands)
    assert [e["sequence"] for e in commands] == list(range(1, len(commands) + 1))
    replay = Game(events[0]["seed"])
    for event in commands:
        assert event["accepted"]
        action = event["action"]
        if action == "start":
            replay.start()
        elif action == "draw":
            replay.draw_card()
        elif action == "finish_dealing":
            replay.finish_dealing()
        else:
            replay.act(event["seat"], action, event["ids"])
    assert replay.result == result["result"]
    assert replay.history == sim.game.history
    assert replay.bottom == sim.game.bottom
    with Simulation(seed=42, strategies=BASIC) as again:
        assert again.run_round() == result


def test_manual_commands_rejections_and_custom_observation(tmp_path):
    path = tmp_path / "trace.jsonl"
    with Simulation(3, BASIC, path, trace=True) as sim:
        sim.command(None, "start")
        sim.command(None, "draw")
        observer = sim.observation(0)
        assert len(observer.hand) == 1 and not observer.known_bottom
        assert observer.counts == (1, 0, 0, 0)
        with pytest.raises(RuleError):
            sim.command(0, "play", [observer.hand[0].id])
        assert len(sim.observation(0).hand) == 1
        sim.step()  # Finish dealing without starting a server or waiting for clocks.
        assert sim.game.phase == "burying"
        seat = sim.game.turn
        action, ids = sim.game.ai_action(seat, "basic")
        sim.command(seat, action, ids)
        assert sim.game.phase == "playing"
        assert len(observer.hand) == 1  # Earlier observations stay immutable.
        sim.run_round()
        previous = sim.results[0]
        sim.run_round()
        assert [r["round"] for r in sim.results] == [1, 2]
        assert sim.results[1]["result"]["dealer"] == previous["result"]["next_dealer"]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rejected = [r for r in rows if r["event"] == "command" and not r["accepted"]]
    assert len(rejected) == 1 and rejected[0]["error"]
    assert sum(r["event"] == "round_result" for r in rows) == 2
    with pytest.raises(RuntimeError, match="closed"):
        sim.step()


def test_full_match_and_limits(tmp_path):
    path = tmp_path / "match.jsonl"
    with Simulation(0, BASIC, path) as sim:
        results = sim.run_match()
        assert sim.game.phase == "match_end"
        assert results[-1]["result"]["champion"] in (0, 1)
        assert results[-1]["level"] == 14
        assert sim.run_match() == results
        with pytest.raises(RuleError):
            sim.run_round()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    match = [r for r in rows if r["event"] == "match_result"]
    assert len(match) == 1 and match[0]["champion"] == results[-1]["result"]["champion"]
    assert sum(r["event"] == "round_result" for r in rows) == len(results)
    with Simulation(0, BASIC) as sim:
        with pytest.raises(RuntimeError, match="step limit"):
            sim.run_round(max_steps=1)
    with Simulation(0, BASIC) as sim:
        with pytest.raises(RuntimeError, match="round limit"):
            sim.run_match(max_rounds=1)


def test_cli_works_without_any_site_packages_and_appends_results(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / "results.jsonl"
    command = [sys.executable, "-S", "-m", "levelup.simulation", "--games", "2", "--seed", "7",
               "--strategies", *BASIC, "--output", str(path)]
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60, check=True)
    summary = json.loads(completed.stdout)
    assert summary["games"] == summary["rounds"] == 2
    assert sum(summary["team_round_wins"]) == 2
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len({r["run_id"] for r in rows}) == 2
    assert [r["seed"] for r in rows if r["event"] == "round_result"] == [7, 8]
    assert not any(r["event"] == "command" for r in rows)


@pytest.mark.parametrize("seat", [-1, 4, True, None, "0"])
def test_engine_rejects_invalid_seats(seat):
    game = Game(0)
    game.start()
    with pytest.raises(RuleError, match="座位"):
        game.act(seat, "bid", [0])


def test_configuration_validation():
    with pytest.raises(ValueError):
        Simulation(strategies=("basic",))
    with pytest.raises(RuleError):
        Simulation(strategies=("missing",) * 4)
    with pytest.raises(ValueError):
        Simulation(seed="not a seed")
