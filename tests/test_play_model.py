from dataclasses import asdict
import json
import subprocess
import sys

import pytest

from levelup.ai import observe, get_strategy
from levelup.ai.candidates import generate_candidates
from levelup.ai.interface import Action
from levelup.ai.play_features import FEATURE_NAMES, context_from_dict, feature_rows
from levelup.ai.rule_based import RuleBasedStrategy
from levelup.ai.xgboost_play import TreeModel, XGBoostPlayStrategy, MODEL_PATH
from levelup.game import Game, Card
from levelup.god_teacher import ExactSolver, exhaustive_actions, fork, label, state_key
from levelup.training import privileged_state
from levelup.train_play import metrics


def small_game():
    g=Game(0)
    g.phase,g.trump,g.level,g.dealer,g.turn='playing','S',2,0,0
    g.hands=[[Card(0,'H',3),Card(1,'H',14)], [Card(2,'H',4),Card(3,'S',3)],
             [Card(4,'H',5),Card(5,'H',13)], [Card(6,'H',6),Card(7,'C',3)]]
    g.bottom=[Card(8,'D',10)]
    return g


def test_features_unchanged_when_hidden_hands_change():
    game=small_game()
    ctx=observe(game,0)
    candidates=generate_candidates(ctx)
    before=feature_rows(ctx,candidates)
    game.hands[1][0],game.hands[2][0]=game.hands[2][0],game.hands[1][0]
    after=observe(game,0)
    assert ctx==after
    assert before==feature_rows(after,candidates)
    assert all(len(row)==len(FEATURE_NAMES) for row in before)
    assert ctx==context_from_dict(json.loads(json.dumps(asdict(ctx))))


def test_teacher_exact_values_and_optimal_labels_match_independent_recursion():
    game=small_game()
    ctx=observe(game,0)
    actions=generate_candidates(ctx)
    sample={'observation':asdict(ctx),'privileged':privileged_state(game),
            'candidates':[{'action':a.kind,'ids':[c.id for c in a.cards]} for a in actions]}
    snapshot=privileged_state(game)
    def brute(g):
        if g.phase in ('round_end','match_end'):
            return g.score
        values=[]
        for a in exhaustive_actions(g):
            child=fork(g)
            child.act(child.turn,'play',[c.id for c in a.cards])
            values.append(brute(child))
        return min(values) if g.turn%2==g.dealer%2 else max(values)
    values=[]
    for a in actions:
        child=fork(game)
        child.act(child.turn,'play',[c.id for c in a.cards])
        values.append(-brute(child))
    result=label(sample)
    assert result['method']=='exhaustive_minimax'
    assert result['candidate_values']==values
    assert result['best_candidate_indices']==[i for i,v in enumerate(values) if v==max(values)]
    assert result['global_best_value']==-brute(game)
    assert result['proven_optimal']==(max(values)==-brute(game))
    assert privileged_state(game)==snapshot


def test_teacher_cache_distinguishes_next_leader():
    game = small_game()
    other = fork(game)
    other.turn = 1
    assert state_key(game) != state_key(other)
    shared = ExactSolver()
    assert shared.value(game) == ExactSolver().value(game)
    assert shared.value(other) == ExactSolver().value(other)


def test_evaluation_uses_action_tiebreak_and_excludes_forced_decisions():
    rows = [
        {'candidates': [{'action': 'play', 'ids': [9]}, {'action': 'play', 'ids': [2]}],
         'teacher': {'best_candidate_indices': [1], 'candidate_values': [10, 30]},
         '_rule_based_choice': 1},
        {'candidates': [{'action': 'play', 'ids': [1]}],
         'teacher': {'best_candidate_indices': [0], 'candidate_values': [None]},
         '_rule_based_choice': 0},
    ]
    report = metrics(rows, [0.5, 0.5, 0.0])
    assert report['decisions'] == 2 and report['nontrivial_decisions'] == 1
    assert report['teacher_top1_agreement'] == report['rule_based_top1_agreement'] == 1
    assert report['mean_teacher_point_regret'] == 0


def test_bidding_and_burying_delegate_exactly_to_rule_based():
    from levelup.simulation import Simulation
    with Simulation(42,('basic',)*4) as sim:
        sim.command(None,'start')
        for _ in range(20): sim.command(None,'draw')
        for ctx in [sim.observation(0)]:
            candidates=generate_candidates(ctx)
            assert XGBoostPlayStrategy().rank(ctx,candidates)==RuleBasedStrategy().rank(ctx,candidates)
        sim.step()
        ctx=sim.observation(sim.game.turn)
        assert ctx.phase=='burying'
        candidates=generate_candidates(ctx)
        assert XGBoostPlayStrategy().rank(ctx,candidates)==RuleBasedStrategy().rank(ctx,candidates)


def test_portable_numeric_trees_match_native_including_split_boundaries(tmp_path):
    np=pytest.importorskip('numpy')
    xgb=pytest.importorskip('xgboost')
    rng=np.random.default_rng(3)
    features=rng.normal(size=(60,len(FEATURE_NAMES))).astype(np.float32)
    data=xgb.DMatrix(features,label=(features[:,0]>0).astype(int),feature_names=list(FEATURE_NAMES))
    data.set_group([6]*10)
    booster=xgb.train({'objective':'rank:pairwise','max_depth':3,'nthread':1},data,num_boost_round=8)
    path=tmp_path/'test.json'
    booster.save_model(path)
    model=TreeModel(path)
    extra=[]
    for tree in model.trees:
        for i,left in enumerate(tree['left_children']):
            if left!=-1:
                row=np.zeros(len(FEATURE_NAMES),dtype=np.float32)
                row[tree['split_indices'][i]]=tree['split_conditions'][i]
                extra.append(row)
    probes=np.vstack([features,*extra])
    expected=booster.predict(xgb.DMatrix(probes,feature_names=list(FEATURE_NAMES)))
    assert np.allclose(model.predict(probes),expected,rtol=0,atol=1e-5)


def test_bundled_strategy_ranks_each_candidate():
    assert MODEL_PATH.is_file()
    game=small_game()
    ctx=observe(game,0)
    candidates=generate_candidates(ctx)
    ranked=get_strategy('xgboost_play').rank(ctx,candidates)
    assert {r.action.key for r in ranked}=={a.key for a in candidates}
    assert [r.score for r in ranked]==sorted([r.score for r in ranked],reverse=True)
    metadata = json.loads(MODEL_PATH.with_suffix('.meta.json').read_text())
    splits = [set(part['runs']) for part in metadata['splits'].values()]
    assert all(splits)
    assert sum(map(len, splits)) == len(set.union(*splits))
    assert metadata['features'] == list(FEATURE_NAMES)


def test_xgboost_selfplay_completes_without_third_party_libraries():
    script = """
import sys
from levelup.simulation import Simulation
with Simulation(seed=22000, strategies=('xgboost_play',)*4) as sim:
    result = sim.run_round()
    assert sim.game.phase in ('round_end', 'match_end')
    assert not any(sim.game.hands)
    assert result['result']['team'] in (0, 1)
assert not {'numpy', 'xgboost', 'fastapi', 'uvicorn'} & sys.modules.keys()
"""
    subprocess.run([sys.executable, '-S', '-c', script], check=True, timeout=120)
