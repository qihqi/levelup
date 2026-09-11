"""Offline Tractor simulation API and CLI. No server, sockets or sleeps required."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import uuid

from .ai import DEFAULT_STRATEGY, get_strategy, observe
from .ai.simulation import finish_ai_deal
from .game import Game, RuleError


class Simulation:
    """One seeded game, driven manually or by four registered AI strategies.

    Seats 0/2 and 1/3 are partners. Use as a context manager when logging.
    step() completes dealing or performs one bury/play action; command() gives
    full control, including individual draws. There are no wall-clock timers.
    """

    def __init__(self, seed=None, strategies=(DEFAULT_STRATEGY,) * 4,
                 log_path=None, trace=False):
        if seed is not None and type(seed) is not int:
            raise ValueError("seed must be an integer or None")
        if isinstance(strategies, str) or len(strategies) != 4:
            raise ValueError("Provide exactly four strategy IDs in seat order")
        self.strategies = tuple(get_strategy(s).id for s in strategies)
        self.seed = secrets.randbits(64) if seed is None else seed
        self.game = Game(self.seed)
        self.run_id = uuid.uuid4().hex
        self.trace = trace
        self.results = []
        self.commands = 0
        self._stream = None
        self._closed = False
        if log_path is not None:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("a", encoding="utf-8")
        self._emit("run_start", seed=self.seed, strategies=self.strategies,
                   trace=trace, dealing="incremental_immediate_reactions")

    def _emit(self, event, **fields):
        if self._stream is not None:
            self._stream.write(json.dumps({
                "schema_version": 1, "run_id": self.run_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event, **fields,
            }, ensure_ascii=False) + "\n")
            self._stream.flush()

    def _check_open(self):
        if self._closed:
            raise RuntimeError("Simulation is closed")

    def observation(self, seat):
        """Immutable player-visible context, suitable for a custom AI/trainer."""
        if type(seat) is not int or seat not in range(4):
            raise ValueError("seat must be 0, 1, 2 or 3")
        return observe(self.game, seat)

    def _state(self, seat=None):
        # Engine control commands have no private observer; use public fields.
        view = self.game.view(0 if seat is None else seat)
        if seat is None:
            view.pop("hand")
        return deepcopy(view)

    def command(self, seat, action, ids=None):
        """Apply a player bid/pass/bury/play or an explicit engine control command.

        Controls: command(None, 'start'/'draw'/'finish_dealing'/'next').
        Player commands: command(seat, 'bid'/'bury'/'play', [card IDs]);
        command(seat, 'pass') confirms no bid after all cards have been drawn.
        RuleError is preserved and rejected commands are logged in trace mode.
        """
        self._check_open()
        if seat is not None and (type(seat) is not int or seat not in range(4)):
            raise RuleError("座位须为 0–3。")
        before = self._state(seat) if self.trace else None
        self.commands += 1
        payload = {"sequence": self.commands, "seat": seat, "action": action, "ids": ids}
        try:
            controls = {"start": self.game.start, "draw": self.game.draw_card,
                        "finish_dealing": self.game.finish_dealing, "next": self.game.next_round}
            if action in controls:
                if seat is not None or ids is not None:
                    raise RuleError("Control commands require seat=None and no cards")
                # Continuing rounds must apply the result's next dealer.
                if action == "start" and self.game.phase != "lobby":
                    raise RuleError("Use next to start the next round")
                controls[action]()
            else:
                self.game.act(seat, action, ids)
        except RuleError as exc:
            if self.trace:
                self._emit("command", **payload, accepted=False, error=str(exc), before=before)
            raise
        if self.trace:
            self._emit("command", **payload, accepted=True, before=before, after=self._state(seat))
        if self.game.phase in ("round_end", "match_end"):
            result = {"seed": self.seed, "round": self.game.round,
                      "strategies": list(self.strategies), "trump": self.game.trump,
                      "level": self.game.level, "tricks": len(self.game.history),
                      "result": deepcopy(self.game.result)}
            self.results.append(result)
            self._emit("round_result", **result)
            if self.game.phase == "match_end":
                self._emit("match_result", champion=self.game.result["champion"],
                           rounds=self.game.round, levels=list(self.game.levels))
        return self._state(seat)

    def step(self):
        """Advance a lobby, dealing phase, or one AI turn; stop at round end."""
        self._check_open()
        if self.game.phase == "lobby":
            self.command(None, "start")
        elif self.game.phase == "dealing":
            finish_ai_deal(self.game, self.strategies, command=self.command)
        elif self.game.phase in ("burying", "playing"):
            seat = self.game.turn
            action, ids = self.game.ai_action(seat, self.strategies[seat])
            self.command(seat, action, ids)
        return self.game.phase

    def run_round(self, max_steps=300):
        """Finish the current round, or begin the next if already at round_end."""
        self._check_open()
        if self.game.phase == "match_end":
            raise RuleError("Match already finished")
        if self.game.phase == "round_end":
            self.command(None, "next")
        for _ in range(max_steps):
            self.step()
            if self.game.phase in ("round_end", "match_end"):
                return deepcopy(self.results[-1])
        self._emit("simulation_error", message="Round step limit exceeded", round=self.game.round)
        raise RuntimeError("Round step limit exceeded")

    def run_match(self, max_rounds=200):
        """Play through A. A safety limit raises instead of reporting a false win."""
        self._check_open()
        for _ in range(max_rounds):
            if self.game.phase == "match_end":
                return deepcopy(self.results)
            self.run_round()
        if self.game.phase == "match_end":
            return deepcopy(self.results)
        self._emit("simulation_error", message="Match round limit exceeded", round=self.game.round)
        raise RuntimeError("Match round limit exceeded")

    def close(self):
        if not self._closed:
            self._emit("run_end", completed_rounds=len(self.results), phase=self.game.phase)
            if self._stream is not None:
                self._stream.close()
            self._closed = True

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *_):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=1, help="Independent seeded games")
    parser.add_argument("--seed", type=int, default=0, help="First seed; subsequent games increment it")
    parser.add_argument("--strategies", nargs=4, default=[DEFAULT_STRATEGY] * 4,
                        metavar=("SOUTH", "EAST", "NORTH", "WEST"))
    parser.add_argument("--match", action="store_true", help="Play full matches instead of one round")
    parser.add_argument("--max-rounds", type=int, default=200)
    parser.add_argument("--output", type=Path, default=Path("logs/simulations.jsonl"))
    parser.add_argument("--trace", action="store_true", help="Include commands and private player views")
    args = parser.parse_args()
    if args.games < 1 or args.max_rounds < 1:
        parser.error("--games and --max-rounds must be positive")
    try:
        for strategy in args.strategies:
            get_strategy(strategy)
    except RuleError as exc:
        parser.error(str(exc))
    wins, champions, rounds = [0, 0], [0, 0], 0
    for seed in range(args.seed, args.seed + args.games):
        with Simulation(seed, args.strategies, args.output, args.trace) as sim:
            if args.match:
                sim.run_match(args.max_rounds)
                champions[sim.game.result["champion"]] += 1
            else:
                sim.run_round()
            for result in sim.results:
                wins[result["result"]["team"]] += 1
            rounds += len(sim.results)
    print(json.dumps({"games": args.games, "rounds": rounds, "team_round_wins": wins,
                      "team_match_wins": champions, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
