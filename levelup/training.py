"""Versioned, JSON-compatible snapshots for offline self-play datasets."""
from dataclasses import asdict
from functools import lru_cache
import gzip
from hashlib import sha256
import json
from pathlib import Path

from .ai.candidates import EXACT_LIMIT, LOCAL_LIMIT


@lru_cache(maxsize=1)
def metadata():
    root = Path(__file__).parent
    sources = [root / 'game.py', root / 'simulation.py', root / 'training.py',
               *sorted((root / 'ai').glob('*.py'))]
    digest = sha256()
    for source in sources:
        digest.update(source.relative_to(root).as_posix().encode())
        digest.update(b'\0' + source.read_bytes() + b'\0')
    return {
        'source_sha256': digest.hexdigest(),
        'candidate_generator': {'id': 'bounded_v1', 'exhaustive': False,
                                'exact_limit': EXACT_LIMIT, 'local_limit': LOCAL_LIMIT},
        'reward': 'terminal round win: +1 for winning team, -1 for losing team',
    }


def observation(context):
    # Only dataclass fields: cached Rules/Knowledge never enter the record.
    return asdict(context)


def privileged_state(game):
    """Teacher/critic information. Never passed to an AIStrategy."""
    return {'hands': [[asdict(c) for c in hand] for hand in game.hands],
            'bottom': [asdict(c) for c in game.bottom],
            'draw_pile': [asdict(c) for c in game.draw_pile]}


def candidate_records(ranked, epsilon=None):
    return [{**r.json(), 'index': i, 'score': r.score,
             'cards': [asdict(c) for c in r.action.cards],
             'selection_probability': None if epsilon is None else
                 epsilon / len(ranked) + (1 - epsilon if i == 0 else 0)}
            for i, r in enumerate(ranked)]


def iter_decisions(path):
    """Stream completed, accepted decision samples joined to transitions/rewards.

    Rejected commands and unfinished rounds are excluded. Wait decisions have
    transition=None. Memory is bounded by pending rounds, not the whole file.
    """
    path = Path(path)
    pending = {}
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            event = row['event']
            if event == 'decision':
                if row['schema_version'] != 2:
                    raise ValueError('Unsupported training schema version')
                pending[row['decision_id']] = {**row, 'transition': None}
            elif event == 'command' and row.get('decision_id') in pending:
                if row['accepted']:
                    pending[row['decision_id']]['transition'] = row
                else:
                    del pending[row['decision_id']]
            elif event == 'decision_outcome':
                sample = pending.pop(row['decision_id'], None)
                if sample is not None:
                    yield {**sample, 'outcome': row}
            elif event == 'run_end':
                pending = {key: value for key, value in pending.items() if value['run_id'] != row['run_id']}
