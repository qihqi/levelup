from collections import Counter
from dataclasses import asdict
import gzip
import json
from pathlib import Path
import subprocess
import sys

import pytest

from levelup.ai import observe
from levelup.game import Card, Game, RuleError
from levelup.simulation import Simulation
from levelup.training import iter_decisions, privileged_state

BASIC = ('basic',) * 4


def read_rows(path):
    with (gzip.open(path, 'rt') if path.suffix == '.gz' else path.open()) as stream:
        return [json.loads(line) for line in stream]


def normalized(value):
    return json.loads(json.dumps(value))


def test_training_replays_decisions_hands_candidates_and_actual_plays(tmp_path, monkeypatch):
    from levelup import simulation
    rank = simulation.rank_actions
    calls = []

    def counted(context, strategy):
        assert not hasattr(context, 'hands')
        calls.append(context.seat)
        return rank(context, strategy)

    monkeypatch.setattr(simulation, 'rank_actions', counted)
    path = tmp_path / 'training.jsonl'
    with Simulation(42, BASIC, path, training=True) as sim:
        result = sim.run_round()
        assert sim.trace
    rows = read_rows(path)
    assert {r['schema_version'] for r in rows} == {2}
    assert rows[0]['candidate_generator']['exhaustive'] is False
    assert len(rows[0]['source_sha256']) == 64
    decisions = {r['decision_id']: r for r in rows if r['event'] == 'decision'}
    assert len(decisions) == len(calls) == sim.decisions  # Exactly one ranking per AI decision.
    assert {d['observation']['phase'] for d in decisions.values()} == {'dealing','burying','playing'}
    replay = Game(42)
    linked = set()
    for row in rows:
        if row['event'] == 'decision':
            assert row['observation'] == normalized(asdict(observe(replay, row['seat'])))
            assert row['privileged'] == privileged_state(replay)
            assert row['observation']['hand'] == row['privileged']['hands'][row['seat']]
            assert 'hands' not in row['observation'] and 'draw_pile' not in row['observation']
            assert len(row['privileged']['hands']) == 4
            assert row['chosen']['candidate_index'] == 0
            assert row['chosen']['ids'] == row['candidates'][0]['ids']
            assert sum(c['selection_probability'] for c in row['candidates']) == 1
            for c in row['candidates']:
                assert c['ids'] == [card['id'] for card in c['cards']]
                assert set(c['ids']) <= {card.id for card in replay.hands[row['seat']]}
                if c['action'] == 'play':
                    cards = [Card(**card) for card in c['cards']]
                    if replay.trick:
                        replay.rules.validate_follow(replay.hands[row['seat']], cards, replay.trick[0][1])
                    else:
                        replay.rules.components(cards)
        elif row['event'] == 'command':
            assert row['accepted']
            before = list(replay.hands[row['seat']]) if row['seat'] is not None else []
            if row['seat'] is None:
                {'start': replay.start, 'draw': replay.draw_card,
                 'finish_dealing': replay.finish_dealing}[row['action']]()
            else:
                decision = decisions[row['decision_id']]
                linked.add(row['decision_id'])
                assert decision['chosen']['action'] == row['action']
                assert decision['chosen']['ids'] == row['ids']
                replay.act(row['seat'], row['action'], row['ids'])
            assert row['privileged_after'] == privileged_state(replay)
            if row['action'] == 'play':
                assert row['actual_play'] == [c.json() for c in before if c not in replay.hands[row['seat']]]
            if row['completed_trick']:
                assert row['completed_trick'] == replay.history[-1]
    assert replay.result == result['result']
    assert linked == {key for key, d in decisions.items() if d['execution'] == 'command'}
    assert any(d['execution'] == 'wait' for d in decisions.values())
    outcomes = [r for r in rows if r['event'] == 'decision_outcome']
    assert Counter(r['decision_id'] for r in outcomes) == Counter(decisions.keys())
    for row in outcomes:
        d = decisions[row['decision_id']]
        assert row['round_id'] == d['round_id']
        assert row['reward'] == (1 if d['seat'] % 2 == result['result']['team'] else -1)
    with Simulation(42, BASIC) as unlogged:
        assert unlogged.run_round() == result


def test_seeded_exploration_reproducible_and_independent_of_deck_rng(tmp_path):
    runs = []
    for index, epsilon in enumerate((1.0, 1.0, 0.0)):
        path = tmp_path / f'{index}.jsonl'
        with Simulation(17, BASIC, path, training=True, exploration=epsilon, policy_seed=123) as sim:
            sim.run_round()
        runs.append(read_rows(path))
    compact = lambda rows: [(r['observation'],r['candidates'],r['chosen'],r['behavior'])
                            for r in rows if r['event'] == 'decision']
    assert compact(runs[0]) == compact(runs[1])
    assert any(r['chosen']['candidate_index'] > 0 for r in runs[0] if r['event'] == 'decision')
    for row in runs[0]:
        if row['event'] == 'decision':
            assert row['behavior']['explored']
            assert row['behavior']['selection_probability'] == pytest.approx(1 / len(row['candidates']))
    starts = [next(r for r in rows if r['event'] == 'command' and r['action'] == 'start') for rows in runs]
    assert starts[0]['privileged_after'] == starts[2]['privileged_after']


def test_external_failed_throw_logs_actual_cards_and_rejections(tmp_path):
    path = tmp_path / 'manual.jsonl'
    with Simulation(0, BASIC, path, training=True) as sim:
        game = sim.game
        game.phase, game.trump, game.round = 'playing', 'S', 1
        game.hands = [[Card(0,'H',3),Card(1,'H',9)], [Card(2,'H',8),Card(3,'C',4)],
                      [Card(4,'D',4),Card(5,'C',5)], [Card(6,'D',6),Card(7,'D',7)]]
        sim.command(0, 'play', [0,1])
        assert [c.id for c in game.hands[0]] == [1]
        with pytest.raises(RuleError):
            sim.command(1, 'play', [999])
        for invalid in (3, [2,'bad'], [[2]], True):
            with pytest.raises(RuleError):
                sim.command(1, 'play', invalid)
    rows = read_rows(path)
    decision = next(r for r in rows if r['event'] == 'decision')
    assert decision['behavior']['kind'] == 'external'
    assert decision['behavior']['selection_probability'] is None
    command = next(r for r in rows if r['event'] == 'command')
    assert command['ids'] == [0,1] and [c['id'] for c in command['actual_play']] == [0]
    assert command['decision_id'] == decision['decision_id']
    assert sum(r['event'] == 'command' and not r['accepted'] for r in rows) == 5
    assert not any(r['event'] == 'decision_outcome' for r in rows)  # No invented reward for unfinished rounds.


def test_cli_training_without_site_packages_and_gzip_append(tmp_path):
    path = tmp_path / 'training.jsonl.gz'
    result = subprocess.run([sys.executable, '-S', '-m', 'levelup.simulation', '--games', '2',
                             '--strategies', *BASIC, '--training', '--exploration', '.15',
                             '--output', str(path)], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=True, timeout=60)
    summary = json.loads(result.stdout)
    rows = read_rows(path)
    assert summary['training_decisions'] == sum(r['event'] == 'decision' for r in rows)
    assert len({r['run_id'] for r in rows}) == 2
    assert sum(r['event'] == 'round_result' for r in rows) == 2


def test_multiple_rounds_have_distinct_groups_and_one_reward_per_decision(tmp_path):
    path = tmp_path / 'rounds.jsonl.gz'
    with Simulation(42, BASIC, path, training=True) as sim:
        sim.run_round()
        sim.run_round()
    rows = read_rows(path)
    decisions = {r['decision_id']: r for r in rows if r['event'] == 'decision'}
    assert len({r['round_id'] for r in decisions.values()}) == 2
    outcomes = [r for r in rows if r['event'] == 'decision_outcome']
    assert Counter(r['decision_id'] for r in outcomes) == Counter(decisions.keys())
    assert all(r['round_id'] == decisions[r['decision_id']]['round_id'] for r in outcomes)
    samples = list(iter_decisions(path))
    assert len(samples) == len(decisions)
    for sample in samples:
        if sample['execution'] == 'wait':
            assert sample['transition'] is None
        else:
            assert sample['transition']['decision_id'] == sample['decision_id']
        assert sample['outcome']['decision_id'] == sample['decision_id']


@pytest.mark.parametrize('kwargs', [{'training':True}, {'exploration':-0.1}, {'exploration':1.1},
                                   {'exploration':float('nan')}, {'exploration':True}, {'policy_seed':True}])
def test_invalid_training_configuration(kwargs):
    with pytest.raises(ValueError):
        Simulation(**kwargs)
