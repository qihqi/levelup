# OpenAI Agent AI

Select **OpenAI Agent AI** (`openai_agent`) in the lobby or between deals. It uses
the official `openai` Python SDK (`AsyncOpenAI`) and Responses API to choose a play.
The default model is `gpt-5-mini`; change it with `LEVELUP_OPENAI_MODEL`. The model
must support Responses and strict JSON-schema outputs. The default remains the
local rule-based strategy. This agent's playing strength has not been benchmarked.

## Setup

From the repository root, install the optional dependency into your existing environment:

```bash
uv pip install --python "$HOME/.venv/bin/python" -e '.[agent]'
export OPENAI_API_KEY="your-api-key"
export LEVELUP_OPENAI_MODEL="gpt-5-mini"  # optional
LEVELUP_PORT=8768 ./run.sh
```

Set the key in the server's terminal/environment, never in frontend JavaScript or
a committed file. Restart your existing server and refresh the browser to load
the new option. Restarting clears in-memory rooms. For an installed wheel, install
the `agent` extra too: `python -m pip install '/path/to/levelup-0.1.0-py3-none-any.whl[agent]'`.
Other strategies work without this extra or an API key.

Selecting this strategy uses the server's API key for AI players, trustee moves
and requested playing hints. Each non-forced play/hint makes at most one API
request; normal API usage charges apply. Missing configuration or an unavailable
API falls back to the rule-based strategy. Check the server's fallback warnings
if the agent appears to play instantly; hints also display a fallback notice.

## What the agent sees and returns

`levelup/ai/openai_agent.py` contains the versioned rules prompt and explicit
`prompt_state()` serializer. Every decision gets a fresh, self-contained request:

- Concise rules for this table: teams, trump and level cards, pairs/tractors,
  following obligations, points, the 80-point target, and bottom multipliers.
- Its seat, partner, dealer, attacking/defending role, current level and trump,
  attacker score, its hand, and all four players' remaining card counts.
- Every completed trick in this deal, with plays, winner and points; the current
  trick and current winner; all public trump declarations.
- The buried bottom only when the acting player is the dealer.
- Legal candidate plays, followed by “What cards do you want to play now?”

Cards use compact names (`SA`, `D10`, `SJ`/`BJ` for small/big joker), with repeated
faces representing duplicate cards. The hand also includes effective suits and
strengths so the level rank is unambiguous. Candidate copies with identical faces
are collapsed; one meaningful choice skips the API call.

The response is strict JSON: `candidate_id`, one short private tactical `reason`
in Chinese, and an optional public `reaction` (empty string for silence). Reactions
must contain fewer than 10 characters including punctuation; oversized or invalid
comments are discarded without affecting the chosen play. The prompt asks for
Chinese table comments such as “先拿一墩”, without revealing remaining cards,
bottom or future plans. Once the AI plays, its reaction appears beside its cards
and persists in the previous-trick recap and enlarged detail. Other strategies,
forced moves and fallback moves show no reaction. The reaction is included in
agent logs; private reasons remain separate from public table state.

The ID is constrained to the supplied choices and revalidated locally.
The selected candidate is promoted above the existing rule ranking, and every
original candidate remains in the returned list. The usual game engine then
checks and applies the move. These ranking scores are not win probabilities.
The candidate generator is bounded: this does not enumerate every possible throw
or complicated follow combination.

No hidden opponent hands, player names, reconnect tokens, or credentials enter
the game prompt. API credentials are used only for SDK authentication. Requests
set `store=False` and do not use conversation IDs or previous response IDs;
different seats never share private conversation state. The agent receives no
external tools. It chooses a play in one model request rather than running an
unbounded tool loop.

## Timing, fallback and interface

The default maximum is 28 seconds per decision, reduced when the room timer has
less time remaining. The web path uses asynchronous SDK I/O outside the room
lock and cancels stale requests after manual play, a turn change, disconnection,
or room cleanup. The existing “AI 正在思考…” indicator applies. Manual play and
WebSocket ping remain available while awaiting a hint.

No SDK retries are allowed. Timeouts, API errors, refusals, incomplete output or
invalid choices return the local rule ranking. An already-expired human timer
uses rules immediately. Bidding and burying always use the rule-based strategy.
`max_output_tokens=2048` bounds output; GPT-5 model names also receive low
reasoning effort. Different compatible models may have different latencies.

The agent implements `AIStrategy.rank(context, candidates)` for synchronous
`Game`/`Simulation` callers, and exposes asynchronous `decide(...)` for the server
or another async host. Use `await strategy.decide(...)` inside an active event
loop; the synchronous entry uses `asyncio.run`. External training commands use
`rank_external()` with rule scores so recording a human-supplied move does not
trigger a billable API request.

## Offline simulation and diagnostics

No web server is required, but this strategy does require network access to
OpenAI for model inference:

```bash
"$HOME/.venv/bin/python" -m levelup.simulation \
  --games 1 --seed 32002 \
  --strategies openai_agent rule_based openai_agent rule_based \
  --training --output logs/agent-training.jsonl
```

Training decision records include `agent`: prompt version, model, status,
chosen card IDs, brief explanation, request ID, token usage and elapsed time
when available. `agent.model_output` preserves the exact `output_text`, structured
output items (including refusal messages), response ID, actual response model,
response status and incomplete details. It is captured before parsing/validation,
so malformed or incomplete model replies are logged too. API failures before a
model response arrives have no `model_output` field. Forced plays, local fallback and external commands are identified
explicitly. A generated AI command reuses its decision record without another
request. Usual training snapshots still include all hands for later teachers;
those privileged fields are never sent to the agent.

Web games record `agent_decision` events in existing server-only
`logs/websocket-<pid>.jsonl` files, correlated with room, deal, seat and version.
Hints carry `agent` metadata in their logged WebSocket response. Autonomous
tactical explanations are not broadcast to opponents. SDK error bodies, request
headers and outgoing prompts are not logged by this integration.

Tests use the real SDK with `httpx.MockTransport`: structured selection,
information isolation, configuration, refusals/errors, deadlines and cancellation,
WebSocket hints, automatic AI play, training logging and a complete AI round.
No live API call was made during implementation because no API key was configured.

Official OpenAI documentation:
[Python SDK](https://developers.openai.com/api/docs/libraries),
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[GPT-5 Mini](https://developers.openai.com/api/docs/models/gpt-5-mini).
