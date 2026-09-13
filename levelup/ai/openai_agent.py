"""A stateless card-playing agent using the optional official OpenAI SDK.

Only AIContext enters the prompt. Each decision starts a fresh request, so
different seats never share conversation state or private cards.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import time

from ..game import RANKS
from .interface import AIStrategy, RankedAction, Reason, ordered
from .rule_based import RuleBasedStrategy

log = logging.getLogger('levelup.agent')
DEFAULT_MODEL = 'gpt-5-mini'
PROMPT_VERSION = 'tractor-play-v2'

RULES = """You are a player in four-player, two-deck 升级 (Tractor). Choose the
play that gives YOUR TEAM the best chance of winning this deal. These are this
table's rules, even if you know other variants:
- Seats are 0,1,2,3 clockwise; opposite seats are partners. There are two copies
  of each card (108 total), originally 25 per player and 8 bottom cards. The
  dealer's team defends; the other team attacks. Players cannot communicate.
- Trump includes the named trump suit, ALL level-rank cards, and both jokers.
  In no-trump, only level cards and jokers are trump. Strength high to low:
  big joker > small joker > trump-suit level > other level cards (equal) >
  A K Q J 10 ... 2, skipping the level rank. Ordinary suits use that rank order.
  A level card's printed suit is NOT its effective suit. Equal strength loses
  to an earlier play. Identical pairs require identical printed suit AND rank.
- Lead singles, pairs, consecutive pairs (tractors), or same-effective-suit
  combinations (throws). The level-rank gap is compressed for consecutive pairs.
  Follow the lead's effective suit and count as far as possible; match tractors
  and pairs as required before filling with singles. If entirely void, only a
  shape-matching all-trump play can ruff. Otherwise discards cannot win. A failed
  throw is reduced by the engine to a beatable component, including when partner
  could beat it. The supplied candidate list is authoritative for legal choices.
- The trick winner leads next. 5 is worth 5; 10 and K are worth 10; all other
  cards 0. Attackers collect points they win. If attackers win the final trick,
  add bottom points times the LEAD shape's multiplier: single 2, pair 4,
  n-pair tractor 2n+2; all-single throw 3, other throws use the longest component.
  Defenders win below 80 points; attackers win at 80+. Defenders gain 3 levels
  for 0, 2 for 5-35, 1 for 40-75. Attackers gain floor((score-80)/40) at 80+.
- You see your own hand, public declarations, every completed trick, the current
  trick and remaining hand counts. Only the dealer knows the buried bottom.
  Infer missing suits and remaining masters from public plays; never assume you
  know an opponent's hand. Cash safe masters, preserve useful pairs/tractors,
  cooperate with partner, and weigh points, control, trump economy and last trick.
Return one candidate_id and a brief tactical reason (one sentence, in Chinese).
Optionally add a public reaction about this play, in Chinese and STRICTLY fewer
than 10 characters including punctuation, e.g. "先拿一墩" or "这分我收了".
Use an empty string for reaction when you have nothing to say. Everyone at the
table sees this reaction: never disclose remaining cards, bottom or future plans.
The reason is private; the reaction is a separate short comment, not your reasoning.
Candidate order is arbitrary, not a recommendation. Do not invent cards or plays.
"""


def face(card):
    return {15: 'SJ', 16: 'BJ'}[card.rank] if card.suit == 'J' else card.suit + RANKS[card.rank]


def prompt_state(context, candidates):
    """Compact, explicit allowlist; no Game, player names, tokens or hidden hands."""
    rules = context.rules

    def plays(trick):
        return [{'seat': p.seat, 'cards': [face(c) for c in p.cards]} for p in trick]

    def winner(trick):
        return rules.winner([(p.seat, list(p.cards)) for p in trick]) if trick else None

    state = {
        'notation': 'S=spades,H=hearts,C=clubs,D=diamonds; SJ=small joker,BJ=big joker; repeated faces are copies',
        'seat': context.seat, 'partner': context.partner, 'dealer': context.dealer,
        'role': 'defender' if context.defending else 'attacker',
        'level': RANKS[context.level], 'trump_suit': context.trump,
        'attacker_points': context.score, 'remaining_counts': list(context.counts),
        'hand': [{'card': face(c), 'effective_suit': rules.suit(c), 'strength': rules.strength(c)}
                 for c in rules.sort(context.hand)],
        'strength_note': 'T=trump; larger strength wins within the same effective suit',
        'declarations': [{'seat': b.seat, 'cards': [face(c) for c in b.cards]}
                         for b in context.declarations],
        'previous_tricks': [{'plays': plays(t), 'winner': winner(t),
                             'points': sum(c.points for p in t for c in p.cards)}
                            for t in context.history],
        'current_trick': plays(context.trick), 'current_winner': winner(context.trick),
        'candidates': [{'candidate_id': i, 'cards': [face(c) for c in a.cards]}
                       for i, a in enumerate(candidates)],
    }
    if context.seat == context.dealer:
        state['known_bottom'] = [face(c) for c in context.known_bottom]
    return state


def response_format(count):
    return {'type': 'json_schema', 'name': 'tractor_play', 'strict': True,
            'schema': {'type': 'object', 'properties': {
                'candidate_id': {'type': 'integer', 'enum': list(range(count))},
                'reason': {'type': 'string'},
                'reaction': {'type': 'string', 'maxLength': 9,
                             'description': 'Optional public Chinese reaction, 0–9 characters; empty means silence.'}},
                'required': ['candidate_id', 'reason', 'reaction'], 'additionalProperties': False}}


def public_reaction(value):
    """An invalid optional comment must not invalidate an otherwise valid play."""
    if not isinstance(value, str):
        return ''
    value = value.strip()
    return value if len(value) < 10 and all(c.isprintable() for c in value) else ''


def make_client(seconds):
    # Lazy import keeps rule-based games and offline simulations stdlib-only.
    from openai import AsyncOpenAI
    return AsyncOpenAI(timeout=seconds, max_retries=0)


@dataclass
class AgentResult:
    ranked: list[RankedAction]
    report: dict


class AgentRankings(list):
    def __init__(self, result):
        super().__init__(result.ranked)
        self.agent_report = result.report


class OpenAIAgentStrategy(AIStrategy):
    id = 'openai_agent'
    name = 'OpenAI Agent AI'
    description = '大模型根据本桌规则、自己的手牌和全部公开出牌选牌；需服务器配置 OpenAI API，失败时由记牌策略接管。亮主与扣底沿用记牌策略。'

    def __init__(self, model=None, seconds=28.0):
        if not 0 <= seconds <= 28:
            raise ValueError('Agent requires 0–28 seconds')
        self.model, self.seconds = model, seconds

    def rank(self, context, candidates):
        # Synchronous entry for Game and standalone Simulation. The web server
        # calls decide() directly so network waits are cancellable and nonblocking.
        if context.phase != 'playing':
            return RuleBasedStrategy().rank(context, candidates)
        return AgentRankings(asyncio.run(self.decide(context, candidates)))

    def rank_external(self, context, candidates):
        return AgentRankings(AgentResult(RuleBasedStrategy().rank(context, candidates),
                             {'status': 'external', 'candidate_ranking': 'rule_based',
                              'reason': 'no_inference_for_external_command'}))

    async def decide(self, context, candidates, *, seconds=None, cancel=None):
        start = time.monotonic()
        budget = self.seconds if seconds is None else min(self.seconds, max(0, seconds))
        baseline = RuleBasedStrategy().rank(context, candidates)
        model = self.model or os.environ.get('LEVELUP_OPENAI_MODEL') or DEFAULT_MODEL
        report = {'prompt_version': PROMPT_VERSION, 'model': model, 'status': 'fallback',
                  'candidate_count': len(candidates)}

        def finish(ranked, reason):
            report.update(reason=reason, elapsed_seconds=round(time.monotonic() - start, 4))
            # Do not log SDK exception bodies, credentials, or raw HTTP payloads.
            if report['status'] == 'fallback':
                log.warning('OpenAI agent fallback: %s', reason)
            return AgentResult(ranked, report)

        if context.phase != 'playing':
            report['status'] = 'rule_based'
            return finish(baseline, 'rule_based_phase')
        # Physical copies of the same play do not warrant separate model calls.
        unique = {}
        for a in candidates:
            unique.setdefault(tuple(sorted(c.key for c in a.cards)), a)
        choices = tuple(unique.values())
        report['unique_candidate_count'] = len(choices)
        if len(choices) <= 1:
            report['status'] = 'forced'
            return finish(baseline, 'single_candidate')
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError()
        if budget <= 0:
            return finish(baseline, 'no_time')
        if not os.environ.get('OPENAI_API_KEY'):
            return finish(baseline, 'missing_api_key')
        try:
            # One deadline covers client setup, request, response and close. No
            # retries: a failed request should not spend another turn's budget.
            async with asyncio.timeout(max(0, budget - (time.monotonic() - start))):
                async with make_client(budget) as client:
                    response = await client.responses.create(
                        model=model, instructions=RULES, store=False,
                        input='What cards do you want to play now? Select exactly one candidate.\n'
                              + json.dumps(prompt_state(context, choices), ensure_ascii=False, separators=(',', ':')),
                        text={'format': response_format(len(choices))},
                        max_output_tokens=2048,
                        **({'reasoning': {'effort': 'low'}} if model.startswith('gpt-5') else {}),
                    )
            report['request_id'] = getattr(response, '_request_id', None)
            # Capture output before validation so malformed JSON, refusals and
            # incomplete generations remain available in decision diagnostics.
            # Only response output is retained, not request headers or input.
            report['model_output'] = {
                'response_id': response.id,
                'model': response.model,
                'status': response.status,
                'output_text': response.output_text,
                'output': [item.model_dump(mode='json', exclude_none=True) for item in response.output],
                'incomplete_details': (response.incomplete_details.model_dump(mode='json', exclude_none=True)
                                       if response.incomplete_details is not None else None),
            }
            if response.usage is not None:
                report['usage'] = {k: getattr(response.usage, k) for k in
                                   ('input_tokens', 'output_tokens', 'total_tokens')}
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError()
            if response.status != 'completed':
                return finish(baseline, 'incomplete_response')
            if any(getattr(part, 'type', None) == 'refusal'
                   for item in response.output for part in getattr(item, 'content', ())):
                return finish(baseline, 'refusal')
            data = json.loads(response.output_text)
            if (not isinstance(data, dict) or set(data) - {'candidate_id', 'reason', 'reaction'}
                    or not {'candidate_id', 'reason'} <= set(data)
                    or type(data['candidate_id']) is not int or not 0 <= data['candidate_id'] < len(choices)
                    or not isinstance(data['reason'], str) or not data['reason'].strip()):
                return finish(baseline, 'invalid_choice')
            choice = choices[data['candidate_id']]
            reason = data['reason'].strip()[:240]
            # Promote the selected supplied action, preserving every other
            # candidate and its rule score. This score is an ordering, not value.
            ranked = [RankedAction(r.action, r.reasons + (
                Reason('agent_choice', reason, baseline[0].score - r.score + 1),))
                if r.action.key == choice.key else r for r in baseline]
            report.update(status='selected', candidate_id=data['candidate_id'],
                          chosen_ids=[c.id for c in choice.cards], explanation=reason,
                          reaction=public_reaction(data.get('reaction')))
            return finish(ordered(ranked), 'model_choice')
        except asyncio.CancelledError:
            raise
        except ImportError:
            return finish(baseline, 'sdk_not_installed')
        except TimeoutError:
            return finish(baseline, 'timeout')
        except (ValueError, TypeError, AttributeError):
            return finish(baseline, 'invalid_response')
        except Exception as exc:
            report['error_type'] = type(exc).__name__
            return finish(baseline, 'api_error')
