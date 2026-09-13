"""Play-only XGBoost strategy with dependency-free inference of bundled trees."""
from functools import lru_cache
import json
import math
from pathlib import Path
import struct

from .interface import AIStrategy, RankedAction, Reason, ordered
from .play_features import FEATURE_NAMES, feature_rows
from .rule_based import RuleBasedStrategy

MODEL_PATH = Path(__file__).resolve().parent.parent / 'models' / 'play_v1.json'


class TreeModel:
    """Evaluate numeric single-output gbtree models exported by XGBoost.

    Training needs XGBoost; running the game needs only this JSON and Python.
    Parity against native XGBoost is verified during every model export.
    """
    def __init__(self, path=MODEL_PATH):
        data = json.loads(Path(path).read_text())['learner']
        if data['objective']['name'] != 'rank:pairwise' or data['gradient_booster']['name'] != 'gbtree':
            raise ValueError('Expected a numeric gbtree pairwise ranker')
        if data['feature_names'] != list(FEATURE_NAMES):
            raise ValueError('Play model feature schema does not match the encoder')
        base = json.loads(data['learner_model_param']['base_score'])
        self.base = float(base[0] if isinstance(base,list) else base)
        self.trees = data['gradient_booster']['model']['trees']
        if any(data['gradient_booster']['model']['tree_info']):
            raise ValueError('Multi-output trees are not supported')
        if any(any(t['split_type']) for t in self.trees):
            raise ValueError('Categorical trees are not supported by the portable evaluator')
        for tree in self.trees:
            conditions = tree['split_conditions']
            tree['split_conditions'] = struct.unpack(f'{len(conditions)}f', struct.pack(f'{len(conditions)}f', *conditions))

    def predict(self, rows):
        scores = []
        for row in rows:
            if len(row) != len(FEATURE_NAMES):
                raise ValueError('Invalid play feature count')
            # The native predictor compares float32 inputs to float32 thresholds.
            values = struct.unpack(f'{len(row)}f', struct.pack(f'{len(row)}f', *row))
            score = self.base
            for tree in self.trees:
                index = 0
                left, right = tree['left_children'], tree['right_children']
                while left[index] != -1:
                    value = values[tree['split_indices'][index]]
                    go_left = tree['default_left'][index] if math.isnan(value) else value < tree['split_conditions'][index]
                    index = left[index] if go_left else right[index]
                score += tree['split_conditions'][index]
            scores.append(score)
        return scores


@lru_cache(maxsize=1)
def bundled_model():
    return TreeModel()


class XGBoostPlayStrategy(AIStrategy):
    id = 'xgboost_play'
    name = 'XGBoost 出牌 AI'
    description = '出牌使用全信息教师训练的小型模型；实战只看自己的手牌和公开信息，亮主与扣底沿用记牌策略。'

    def rank(self, context, candidates):
        if context.phase != 'playing':
            return RuleBasedStrategy().rank(context, candidates)
        scores = bundled_model().predict(feature_rows(context, candidates))
        return ordered(RankedAction(action, (Reason('xgboost_play','XGBoost 出牌评分', score),))
                       for action,score in zip(candidates,scores))
