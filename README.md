# durable-agents

**Your LLM agent issued the refund. Then the pod restarted. Did it issue it twice?**

An event-sourced runtime for LLM agents. Every model call, tool call,
guardrail decision, and human approval is written to an append-only log
*before and after* it happens. Run state is a pure fold over that log —
so when a process dies mid-run, a different process reads the log,
figures out exactly what was in flight, and finishes the job without
repeating side effects that already happened.

```
seq=  0  RunStarted         goal='Refund order A-8891, item arrived damaged.'
seq=  1  LLMCallRequested   step=1
seq=  2  LLMCallFailed      step=1 attempt=1 error='429 Too Many Requests'
seq=  3  LLMCallCompleted   step=1 -> issue_refund({'order_id': 'A-8891', 'amount_inr': 6400})
seq=  4  ToolCallRequested  step=1 issue_refund(...)     ← process killed here
seq=  5  ToolCallCompleted  step=1 issue_refund -> {...} [recovered]
seq=  6  RunCompleted       final_answer='Refund RF-55012 processed.'
```

That `[recovered]` marker is a different process finishing the work.
One refund was created, not two.

---

## Install

```bash
pip install durable-agents
```

Python 3.12+. Postgres is optional — start with the in-memory store.

## Five lines

```python
from durable_agents import Runtime, InMemoryEventStore, tool

@tool(side_effect=True)
async def issue_refund(order_id: str, amount: int, idempotency_key: str) -> dict:
    return await payments.refund(order_id, amount, key=idempotency_key)

runtime = Runtime(store=InMemoryEventStore(), llm=my_llm_client, tools=[issue_refund])
run = await runtime.start(goal="Refund order A-8891, item arrived damaged.")

print(run.state.status)   # 'completed' — or 'awaiting_approval', see below
```

That example is executed by the test suite
(`tests/unit/test_public_api.py`), so it cannot silently rot.

## Make it survive a restart

Swap the store, and create the table once:

```python
from durable_agents import Runtime, PostgresEventStore, create_schema

await create_schema(DATABASE_URL)          # idempotent; or: durable-agents init-db
store = await PostgresEventStore.connect(DATABASE_URL)

runtime = Runtime(store=store, llm=my_llm_client, tools=[issue_refund])
run_id = await runtime.create(goal="Refund order A-8891.")   # record it
state  = await runtime.resume(run_id)                        # run it — now, or after a crash
```

`resume()` is safe to call on a run that finished, one that was killed
mid-tool-call, and one that was never started. Starting really is just
resuming from an almost-empty log.

---

## What you get

**Exactly-once side effects.** Every tool call gets a deterministic
idempotency key — `sha256(run_id + seq + tool + args)` — that is stable
across process restarts. A tool declaring an `idempotency_key`
parameter receives it automatically, and the runtime hands the *same*
key back on a retry, so your backend can deduplicate. The test suite
proves the hard case: kill the process after the payment API call but
*before* the completion is recorded, resume, and the tool really is
called twice while exactly one refund exists.

**Retries that don't lose count.** Transient provider errors (429, 500,
timeouts) are retried with exponential backoff. The attempt count lives
in the event log, not a local variable — so a process that crashes
mid-retry doesn't hand the next one a fresh budget, which is how a
flapping provider plus a crash-loop turns into an infinite retry.

**Human approval as a first-class state.** Mark a tool
`requires_approval=True` (or a predicate over its arguments) and the run
*parks* — it doesn't block a thread, it stops. Approve it tomorrow, from
a different process, and the run resumes exactly where it was.

```python
@tool(requires_approval=lambda args: args["amount"] > 5000, side_effect=True)
async def issue_refund(...): ...

run = await runtime.start(goal="Refund order A-8891.")
if run.state.status == "awaiting_approval":
    print(run.state.pending_approval.tool)      # 'issue_refund'

# ...hours later, in a different process, after a human clicks Approve:
await runtime.approve(run.id, approver="dana@example.com")
final = await runtime.resume(run.id)
```

Deciding and executing are separate on purpose: `approve()` only
records the human's decision (that's what `POST /runs/{id}/approve`
calls), while `resume()` is whatever process owns running agents.
`deny(run_id, approver, reason)` feeds the reason back to the model so
it can choose another action instead of stalling.

**An audit trail you didn't write logging for.** "Who approved this
refund, and what did the agent know at the time?" is
`SELECT * FROM events WHERE run_id = $1 ORDER BY seq`. Three months
later, it still answers.

**A worker, so runs finish themselves.** Durability without this is
only half the story: the log survives, but a run whose process died
sits there until a human notices. A `Worker` polls for runs that need
work — brand new ones, ones a human just approved, and ones that have
gone quiet long enough to be presumed abandoned — and resumes them.

```python
from durable_agents import Worker

worker = Worker(runtime, stale_after_seconds=60.0)
await worker.run_forever()
```

That's the "a pod dies, a minute later something else picks the run up"
story, and the idempotency keys already in the log are what make it
safe to re-run a step whose outcome was never recorded. Spec describes
a worker and a recovery sweeper as separate processes; they're the same
mechanism with a different threshold, so this is one class — run two
instances with different `stale_after_seconds` if you want the split.

**Guardrails, genuinely optional.** Two different things share this
name, and they have opposite characteristics:

*Validation* — is this tool registered, do the arguments match its
declared schema, is this number over a cap you configured, is the agent
stuck repeating one side-effecting action. No false positives; this is
argument checking, not a security opinion. **On by default.**

*Pattern matching* — regexes guessing whether text is trying to
manipulate the model. Measured on the bundled 80-case corpus at a **20%
false positive rate**, and a false positive means a dead run. Ordinary
machine output trips it: a tool returning
`{"error": "system: disk full"}` matches an injection pattern at 0.9
confidence, and tool results are scanned on every step. **Off by
default**, on by name.

```python
runtime = Runtime(store=..., llm=..., guardrail_profile="standard")
```

| profile | attack success | false positives | |
|---|---|---|---|
| `off` | 100% | 0% | nothing runs |
| `validation` | 50% | 0% | **default** — validation only |
| `lenient` | 25% | 5% | + patterns, mostly logging |
| `standard` | 0% | 20% | + patterns, blocks at ≥0.85 |
| `strict` | 0% | 25% | + escalates on any match |

Measured by `tests/guardrails/test_corpus_eval.py`, which you can run.
The default catches the deterministic half of the threat model and none
of the probabilistic half — that's the trade, stated plainly. An
unrecognised profile name raises rather than quietly falling back. See
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) for what each layer does
and where it's weak.

**An HTTP API, optionally** (`pip install durable-agents[api]`):

```python
from durable_agents.api.app import create_app

app = create_app(store, default_max_steps=15, default_guardrail_profile="standard")
```

| | |
|---|---|
| `POST /runs` | Start a run — `{"goal": "...", "requested_by": "..."}`. Records only, like `Runtime.create()`; doesn't block on execution. |
| `GET /runs/{id}` | Current status, pending approval, final answer. |
| `POST /runs/{id}/approve` | `{"approver": "..."}` |
| `POST /runs/{id}/deny` | `{"approver": "...", "reason": "..."}` |

Every mutating endpoint only *records* — same reasoning as
`Runtime.create()` vs `start()`: an agent run can take minutes, and
blocking an HTTP request for that is fragile against timeouts and load
balancers. Something else (a worker, `resume()` in a loop, the
eventual recovery sweeper) does the actual executing.

---

## Try it in 30 seconds

No database needed for the first one:

```bash
git clone https://github.com/yourname/durable-agents && cd durable-agents
uv sync

uv run python examples/quickstart.py              # in-memory, offline, ~20 lines of output
```

Then the durable versions, which need Postgres:

```bash
docker compose up -d
uv run durable-agents init-db

uv run python examples/offboarding_agent.py       # the full story: approval, retry, exactly-once
uv run python examples/demo_retry_recovery.py     # provider errors + a tool timeout, still completes
uv run python examples/demo_guardrail_block.py    # an over-cap refund, blocked before it executes
uv run durable-agents replay <run_id>             # the full trace of any of them
```

[`examples/offboarding_agent.py`](examples/offboarding_agent.py) is the
one to read first — revoking a departing employee's access across
several systems, where one step needs a human, one vendor API fails,
and re-running must not repeat what already happened.

---

## How it works

```
                    ┌──────────────┐
   goal ──────────► │ Orchestrator │ ◄─── reads the log, rebuilds state, decides
                    └──────┬───────┘      one step, records it, repeats
                           │
      ┌────────────────────┼────────────────────┐
      ▼                    ▼                    ▼
  LLMClient            Tool registry        Guardrails
  (your provider)      (@tool)              (4 layers)
      │                    │                    │
      └────────────────────┼────────────────────┘
                           ▼
                    ┌──────────────┐
                    │  EventStore  │  append-only, (run_id, seq) primary key
                    └──────────────┘
```

Three rules the codebase holds to:

1. **The log is append-only.** No `UPDATE`, no `DELETE`. A state change
   is a new row.
2. **`rebuild_state()` is pure.** No I/O, no clock, no randomness. Same
   events in, same state out — which is what makes replay and crash
   recovery the same code path.
3. **Intent is recorded before the action.** A `ToolCallRequested` with
   no matching `Completed` is exactly how a resuming process knows what
   was in flight when the lights went out.

Concurrency control is the `(run_id, seq)` primary key itself. Two
workers racing on the same run can't both write seq 7; the loser
re-reads and re-decides. There's a test that runs two orchestrators
against one Postgres instance to prove it.

---

## Bring your own model

Implement one method:

```python
from durable_agents import LLMClient, LLMResponse

class MyClient(LLMClient):
    async def call(self, messages, tools, system_prompt=""):
        ...
        return LLMResponse(
            content=..., tool_calls=[...], stop_reason=...,
            input_tokens=..., output_tokens=..., cost_usd=Decimal("0.002"),
            latency_ms=..., provider_request_id=...,
        )
```

`ScriptedLLM` ships for tests — a fixed list of responses (or
exceptions, to simulate a flaky provider) with no network and no cost.

For a real model, `OpenAICompatibleClient` ships too (needs
`pip install durable-agents[openai]`):

```python
from durable_agents.llm.openai_compatible import OpenAICompatibleClient

llm = OpenAICompatibleClient(
    base_url="https://api.openai.com/v1",   # or Ollama, vLLM, Groq, OpenRouter, Azure OpenAI...
    model="gpt-4o-mini",
    api_key=os.environ["OPENAI_API_KEY"],
)
```

It's deliberately not vendor-specific: the OpenAI chat-completions wire
format is what most providers — including most local/open-source model
servers — actually speak, so one implementation covers far more ground
than a client tied to one company's SDK. Point `base_url` at whichever
server you use. Retries are the orchestrator's job (see Iteration 24 in
`docs/BUILD_LOG.md`), so this client doesn't retry internally — it
raises, and the runtime's own backoff takes it from there.

### Testing against a real provider

The automated test suite (`pytest`) never touches a network — every
test above uses `ScriptedLLM` or a mocked HTTP transport, on purpose,
so `pip install -e .[dev]` and `pytest` stays free, fast, and
deterministic for anyone who clones this repo.

A second tier exists specifically to catch what a mock can't: whether
the wire format actually holds up against a real server. It's opt-in
only, and skips cleanly if you haven't set a key:

```bash
$env:LLM_API_KEY = "gsk_..."          # a free Groq key works
pytest -m live tests/live -v
```

`pytest` alone (no `-m`) never runs these — the default excludes the
`live` marker, so a normal test run and CI never spend API quota by
accident. Configure `LLM_BASE_URL`/`LLM_MODEL` the same way as the
client above if you're not using Groq.

Three example scripts under `examples/` go further, exercising the
whole runtime — retries, approval parking, resuming from a fresh
process — against a real model:

- `examples/live_smoke_test.py` — the minimal single-tool case
- `examples/live_offboarding.py` — multi-step tool chaining, a vendor
  call that fails once and is retried with the same idempotency key,
  and a destructive step that parks for approval
- `examples/live_incident_triage.py` — a task deliberately built so the
  *obvious* answer is wrong, to see whether the model's reasoning (not
  a script) actually gets it right

---

## Honest limits

Read this part before adopting.

- **One real provider client, generically.** `OpenAICompatibleClient`
  ships and is verified against a real provider (see "Testing against a
  real provider" above) — but there's no dedicated `AnthropicClient` or
  official SDK wrapper for any vendor. If your provider doesn't speak
  the OpenAI wire format, you supply your own `LLMClient`.
- **Postgres or in-memory only.** No SQLite, MySQL, or Redis store.
- **Async only**, Python 3.12+.
- **Recovery is poll-based, not instant.** `Worker` finds abandoned
  runs by looking for ones that have gone quiet, since nothing in the
  log records "a live process is holding this". That means a
  `stale_after_seconds` threshold you have to set above your slowest
  single operation, and if you set it too low two workers race — safe
  (proven by `tests/integration/test_concurrent_workers.py`) but it
  doubles that run's model spend. No leases, no distributed scheduler.
- **The guardrails are pattern-based, not a model.** They catch
  unsophisticated injection and obvious PII; they will not stop a
  determined attacker. The default profile deliberately runs none of
  that pattern matching (50% attack success, 0% false positives) —
  turning it on costs a 20% false positive rate, and a false positive
  is a failed run. Both numbers are measured, and reported on purpose;
  see `docs/THREAT_MODEL.md` for why one of them is genuinely hard to
  fix. If prompt-injection defence is what you came for, this is the
  weakest part of the project and you should treat it as a starting
  point rather than a solution.
- **It wants your agent loop.** Today you build your agent around
  `Runtime`/`Orchestrator` rather than wrapping a loop you already have.
  A wrapper API for incremental adoption is the most-requested-shaped
  thing on the roadmap.

## Compared to

| | durable-agents | Temporal | LangGraph checkpointers |
|---|---|---|---|
| Durable execution | ✅ | ✅ (far more mature) | partial (snapshots) |
| LLM-native events (tokens, cost, tool idempotency) | ✅ | you build it | partial |
| Human approval as a parked state | ✅ | you build it | you build it |
| Audit trail as plain SQL | ✅ | via its own UI/API | ✗ |
| Distributed workers, timers, signals | ✗ | ✅ | ✗ |
| Operational maturity | alpha | production, years | widely used |
| Infrastructure needed | Postgres | a Temporal cluster | your choice |

If you need general-purpose durable execution at scale, use Temporal.
This exists because agent-shaped durability — token/cost accounting,
tool idempotency, approval parking, injection auditing — is work you'd
otherwise rebuild on top of a general engine.

---

## Troubleshooting

**`UnicodeEncodeError` on Windows.** Windows consoles default to a
legacy codepage, so printing an agent's output crashes on any
non-ASCII character. The `durable-agents` CLI handles this itself; for
your own scripts, add this near the top of your entry point:

```python
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

The library never reconfigures your process's stdout on import — that
would be a rude thing for a dependency to do.

## Docs

- [`docs/SPEC.md`](docs/SPEC.md) — the full design document
- [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) — guardrail threat model + measured results
- [`docs/BUILD_LOG.md`](docs/BUILD_LOG.md) — every decision, in order, including the bugs
- [`DECISIONS.md`](DECISIONS.md) — the significant forks, and what was rejected

## License

MIT.
