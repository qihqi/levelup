"""Generate god-mode labels and train/export a compact play-only XGBoost ranker."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from functools import partial
import gzip
import hashlib
import json
from pathlib import Path
import random
import time

from .ai.interface import Action
from .ai.play_features import FEATURE_NAMES, FEATURE_VERSION, context_from_dict, feature_rows
from .ai.rule_based import RuleBasedStrategy
from .game import Card
from .god_teacher import label
from .training import iter_decisions


def label_one(sample, rollouts):
    teaching = label(sample, rollouts)
    ctx = context_from_dict(sample['observation'])
    actions = tuple(Action(c['action'], tuple(Card(**card) for card in c['cards'])) for c in sample['candidates'])
    return {**sample, 'teacher':teaching, 'features':feature_rows(ctx, actions), 'feature_version':FEATURE_VERSION}


def make_labels(source, output, limit=2000, workers=8, rollouts=2):
    if rollouts not in (1,2):
        raise ValueError('Teacher supports one or two distinct rollout policies')
    samples = []
    for r in iter_decisions(source):
        if r['observation']['phase'] == 'playing':
            samples.append({key:r[key] for key in ('decision_id','run_id','round_id','observation',
                                                  'privileged','candidates','chosen','behavior')})
        if len(samples) == limit:
            break
    if len(samples) < limit:
        raise ValueError(f'Need {limit} play decisions, source contains {len(samples)}')
    output.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if output.exists():
        with gzip.open(output, 'rt') as f:
            existing = [json.loads(line) for line in f]
    if any(row['decision_id'] != samples[i]['decision_id'] for i,row in enumerate(existing)):
        raise ValueError('Existing labels belong to a different input or order')
    start = time.monotonic()
    with ProcessPoolExecutor(max_workers=workers) as pool, gzip.open(output, 'at', encoding='utf-8') as stream:
        for count, row in enumerate(pool.map(partial(label_one, rollouts=rollouts), samples[len(existing):], chunksize=1), len(existing)+1):
            stream.write(json.dumps(row, ensure_ascii=False)+'\n')
            stream.flush()
            if count % 25 == 0 or count == limit:
                print(json.dumps({'labeled':count,'total':limit,'elapsed_seconds':round(time.monotonic()-start)}), flush=True)


def metrics(rows, predictions):
    selected, baseline, regret, baseline_regret = [], [], [], []
    offset = 0
    for row in rows:
        n = len(row['candidates'])
        scores = predictions[offset:offset+n]
        choice = min(range(n), key=lambda i:(-float(scores[i]),
                     (row['candidates'][i]['action'], tuple(sorted(row['candidates'][i]['ids'])))))
        best = row['teacher']['best_candidate_indices']
        if len(best) < n:
            selected.append(choice in best)
            baseline.append(row['_rule_based_choice'] in best)
            values = row['teacher']['candidate_values']
            regret.append(max(values)-values[choice])
            baseline_regret.append(max(values)-values[row['_rule_based_choice']])
        offset += n
    return {'decisions':len(rows), 'nontrivial_decisions':len(selected),
            'teacher_top1_agreement':sum(selected)/len(selected) if selected else None,
            'rule_based_top1_agreement':sum(baseline)/len(baseline) if baseline else None,
            'mean_teacher_point_regret':sum(regret)/len(regret) if regret else None,
            'rule_based_mean_teacher_point_regret':sum(baseline_regret)/len(baseline_regret) if baseline_regret else None}


def train(source, model_path):
    import numpy as np
    import xgboost as xgb
    with gzip.open(source, 'rt') as stream:
        rows = [json.loads(line) for line in stream]
    # Recompute the reference policy: future self-play logs may be ordered by a
    # different model, so candidate zero is not necessarily the rule-based choice.
    for row in rows:
        ctx = context_from_dict(row['observation'])
        actions = tuple(Action(c['action'], tuple(Card(**card) for card in c['cards']))
                        for c in row['candidates'])
        best = RuleBasedStrategy().rank(ctx, actions)[0].action.key
        row['_rule_based_choice'] = next(i for i, action in enumerate(actions) if action.key == best)
    # Input UUIDs do not determine split: shuffle games in their recorded order.
    runs = list(dict.fromkeys(r['run_id'] for r in rows))
    random.Random(20260911).shuffle(runs)
    n = len(runs)
    if n < 5:
        raise ValueError('At least five independent games are needed for grouped evaluation')
    test_runs, val_runs = set(runs[:max(2,n//6)]), set(runs[max(2,n//6):2*max(2,n//6)])
    partitions = {'train':[], 'validation':[], 'test':[]}
    for r in rows:
        group = 'test' if r['run_id'] in test_runs else 'validation' if r['run_id'] in val_runs else 'train'
        partitions[group].append(r)
    for name, records in partitions.items():
        if not any(len(r['teacher']['best_candidate_indices']) < len(r['candidates']) for r in records):
            raise ValueError(f'{name} has no nontrivial decisions; collect more independent games')
    def matrix(records, trainable=False):
        if trainable:
            records = [r for r in records if len(r['teacher']['best_candidate_indices']) < len(r['candidates'])]
        if not records:
            raise ValueError('A dataset partition has no usable decisions; collect more independent games')
        features, labels, groups = [], [], []
        for r in records:
            if r['feature_version'] != FEATURE_VERSION:
                raise ValueError('Incompatible feature schema')
            features.extend(r['features'])
            best = r['teacher']['best_candidate_indices']
            labels.extend(int(i in best) for i in range(len(r['candidates'])))
            groups.append(len(r['candidates']))
        data = xgb.DMatrix(np.asarray(features,dtype=np.float32), label=labels, feature_names=list(FEATURE_NAMES))
        data.set_group(groups)
        return data
    train_data = matrix(partitions['train'],True)
    booster = xgb.train({'objective':'rank:pairwise','eval_metric':'ndcg@1','max_depth':3,
                         'eta':.05,'subsample':.85,'colsample_bytree':.9,'min_child_weight':3,
                         'lambda':8,'seed':20260911,'nthread':4,'tree_method':'hist'},
                        train_data, num_boost_round=220, verbose_eval=False)
    # Select size on validation games using the same tie-break as the app.
    validation = matrix(partitions['validation'])
    sizes = (40,80,120,160,220)
    validation_sizes = {size:metrics(partitions['validation'], booster.predict(validation,iteration_range=(0,size))) for size in sizes}
    size = max(sizes,key=lambda n:(validation_sizes[n]['teacher_top1_agreement'],
                                   -validation_sizes[n]['mean_teacher_point_regret'], -n))
    booster = booster[:size]
    model_path.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(model_path)
    report = {'model':'play_v1','feature_version':FEATURE_VERSION,'features':list(FEATURE_NAMES),
              'training_library':xgb.__version__, 'trees':booster.num_boosted_rounds(),'max_depth':3,
              'dataset_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
              'teacher_source_sha256':hashlib.sha256((Path(__file__).parent/'god_teacher.py').read_bytes()).hexdigest(),
              'source_sha256':{name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                               for name in ('train_play.py','ai/play_features.py','ai/rule_based.py',
                                            'ai/knowledge.py','ai/candidates.py','game.py')},
              'validation_model_sizes':validation_sizes,
              'decisions':len(rows), 'candidate_rows':sum(len(r['candidates']) for r in rows),
              'label_methods':{m:sum(r['teacher']['method']==m for r in rows) for m in sorted({r['teacher']['method'] for r in rows})},
              'proven_optimal_labels':sum(r['teacher']['proven_optimal'] for r in rows),
              'splits':{k:{'runs':list(dict.fromkeys(r['run_id'] for r in records)),
                           'trainable_candidate_rows':sum(len(r['candidates']) for r in records
                               if len(r['teacher']['best_candidate_indices']) < len(r['candidates'])),
                           **metrics(records,booster.predict(matrix(records)))} for k,records in partitions.items()},
              'limitations':'Teacher is exact only on small solved endgames or forced moves. Other labels are full-information rollout estimates among bounded candidates. Runtime uses public information only.'}
    from .ai.xgboost_play import TreeModel
    portable = TreeModel(model_path)
    test_features = [features for row in partitions['test'] for features in row['features']]
    expected = booster.predict(matrix(partitions['test']))
    error = max(abs(a-float(b)) for a,b in zip(portable.predict(test_features),expected))
    if error > 1e-4:
        raise AssertionError(f'Portable prediction mismatch: {error}')
    report['portable_max_error'] = error
    model_path.with_suffix('.meta.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='features'},ensure_ascii=False,indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    collect = sub.add_parser('label')
    collect.add_argument('source', type=Path)
    collect.add_argument('output', type=Path)
    collect.add_argument('--limit', type=int, default=2000)
    collect.add_argument('--workers', type=int, default=8)
    collect.add_argument('--rollouts', type=int, default=2, choices=(1,2))
    fit = sub.add_parser('train')
    fit.add_argument('source', type=Path)
    fit.add_argument('--model', type=Path, default=Path(__file__).parent/'models'/'play_v1.json')
    args = parser.parse_args()
    if args.command=='label':
        if args.limit<1 or args.workers<1:
            parser.error('limit and workers must be positive')
        make_labels(args.source,args.output,args.limit,args.workers,args.rollouts)
    else:
        train(args.source,args.model)


if __name__=='__main__':
    main()
