"""Strategy plug-in contract. Strategies receive immutable player-visible data only."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cached_property

from ..game import Card, Rules


@dataclass(frozen=True)
class Play:
    seat: int
    cards: tuple[Card, ...]


@dataclass(frozen=True)
class Bid:
    seat: int
    value: int
    cards: tuple[Card, ...]


@dataclass(frozen=True)
class AIContext:
    seat: int
    phase: str
    hand: tuple[Card, ...]
    level: int
    trump: str | None
    dealer: int
    score: int
    counts: tuple[int, ...]
    trick: tuple[Play, ...] = ()
    history: tuple[tuple[Play, ...], ...] = ()
    bid: Bid | None = None
    declarations: tuple[Bid, ...] = ()
    known_bottom: tuple[Card, ...] = ()

    @cached_property
    def rules(self):
        # A fresh rules object; never a reference to the live Game.
        return Rules(self.level, self.trump)

    @property
    def partner(self):
        return (self.seat + 2) % 4

    @property
    def enemies(self):
        return ((self.seat + 1) % 4, (self.seat + 3) % 4)

    @property
    def defending(self):
        return self.seat % 2 == self.dealer % 2

    @property
    def table(self):
        return [(p.seat, list(p.cards)) for p in self.trick]

    @property
    def remaining_seats(self):
        if not self.trick:
            return tuple((self.seat + i) % 4 for i in range(1, 4))
        return tuple((self.seat + i) % 4 for i in range(1, 4 - len(self.trick)))


@dataclass(frozen=True)
class Action:
    kind: str
    cards: tuple[Card, ...] = ()

    @property
    def key(self):
        return self.kind, tuple(sorted(c.id for c in self.cards))


@dataclass(frozen=True)
class Reason:
    rule: str
    label: str
    value: float


@dataclass(frozen=True)
class RankedAction:
    action: Action
    reasons: tuple[Reason, ...]

    @property
    def score(self):
        return sum(r.value for r in self.reasons)

    def json(self):
        return {"action": self.action.kind, "ids": [c.id for c in self.action.cards],
                "score": round(self.score, 3),
                "reasons": [{"rule": r.rule, "label": r.label, "value": round(r.value, 3)}
                            for r in self.reasons if r.value]}


class AIStrategy(ABC):
    id: str
    name: str
    description: str

    def rank_external(self, context, candidates):
        """Rank candidates when logging a command chosen by an external caller.

        Remote policies override this to avoid billable inference just for logs.
        """
        return self.rank(context, candidates)

    @abstractmethod
    def rank(self, context: AIContext, candidates: tuple[Action, ...]) -> list[RankedAction]:
        """Score every supplied legal candidate, returning highest score first.

        Never mutate the observation, read hidden hands, or invent fresh actions.
        Local policies break ties deterministically; remote policies may be
        stochastic. The caller uses the first-ranked candidate.
        """


def ordered(actions):
    return sorted(actions, key=lambda r: (-r.score, r.action.key))
