# durable-agents

[![CI](https://github.com/therajtiwari/durable-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/therajtiwari/durable-agents/actions/workflows/ci.yml)

An event-sourced runtime for LLM agents, backed by Postgres. Runs survive
process restarts, resume where they stopped without repeating side effects, and
can pause indefinitely when a step needs human approval.

## How it works

Every model call, tool call, and approval is appended to a log before and after
it happens. Run state is a fold over that log, so a process that dies mid-run
leaves enough behind for another one to finish the job:

```
seq=  0  RunStarted         goal='Refund order A-8891, item arrived damaged.'
seq=  1  LLMCallRequested   step=1
seq=  2  LLMCallFailed      step=1 attempt=1 error='429 Too Many Requests'
seq=  3  LLMCallCompleted   step=1 -> issue_refund({'order_id': 'A-8891', 'amount': 6400})
seq=  4  ToolCallRequested  step=1 issue_refund(...)     <- process killed here
seq=  5  ToolCallCompleted  step=1 issue_refund -> {...} [recovered]
seq=  6  RunCompleted       final_answer='Refund RF-55012 processed.'
```

Seq 5 was written by a different process than seq 4. One refund exists, not two.

## Install

```bash
pip install durable-agents
```

Optional extras: `[openai]` for the bundled provider client, `[api]` for the
HTTP endpoints.

## Usage

```python
from durable_agents import Runtime, InMemoryEventStore, tool

@tool(side_effect=True)
async def issue_refund(order_id: str, amount: int, idempotency_key: str) -> dict:
    return await payments.refund(order_id, amount, key=idempotency_key)

runtime = Runtime(store=InMemoryEventStore(), llm=my_llm_client, tools=[issue_refund])
run = await runtime.start(goal="Refund order A-8891, item arrived damaged.")

print(run.state.status)      # 'completed', 'failed', or 'awaiting_approval'
print(run.state.final_answer)
```

The in-memory store dies with the process. For runs that outlive it, swap in
Postgres:

```python
from durable_agents import PostgresEventStore, create_schema

await create_schema(DATABASE_URL)        # idempotent, or run: durable-agents init-db
store = await PostgresEventStore.connect(DATABASE_URL)

runtime = Runtime(store=store, llm=my_llm_client, tools=[issue_refund])
run_id = await runtime.create(goal="Refund order A-8891.")   # record, don't run
state = await runtime.resume(run_id)                         # run it
```

`resume()` is safe to call more than once. On a finished run it returns the
state; on one killed mid-tool-call it reconciles the dangling operation first.

## Writing tools

`@tool` derives the JSON schema from your type hints, so every parameter needs
an annotation. `*args` and `**kwargs` are rejected. The docstring becomes the
description the model sees.

```python
@tool(requires_approval=lambda args: args["amount"] > 5000, side_effect=True)
async def issue_refund(order_id: str, amount: int, idempotency_key: str) -> dict:
    """Refund an order. Needs approval above 5000."""
    ...
```

- A parameter named `idempotency_key` is filled in by the runtime with
  `sha256(run_id + seq + tool + args)`. It is stable across restarts, and a
  retry gets the same key.
- Arguments the function doesn't accept are rejected before it runs, and the
  error goes back to the model to correct.
- Return a `dict` and it is recorded as-is. Anything else is stored as
  `{"result": <value>}`. Values JSON can't represent (`Decimal`, `datetime`,
  `bytes`) are stored as strings.

## Human approval

A tool marked `requires_approval` parks the run rather than blocking on it. No
thread is held and no process stays alive, so the gap can be days.

```python
run = await runtime.start(goal="Refund order A-8891.")
if run.state.status == "awaiting_approval":
    print(run.state.pending_approval.tool)     # 'issue_refund'

# Later, in a different process:
await runtime.approve(run.id, approver="dana@example.com")
final = await runtime.resume(run.id)
```

`approve()` and `deny()` record the decision only; `resume()` does the work. On
denial the reason is passed back to the model, which can then choose another
action.

## Resuming runs automatically

`Worker` polls for runs that need work: new ones, ones a human just approved,
and ones that have been quiet long enough to look abandoned.

```python
from durable_agents import Worker

await Worker(runtime, stale_after_seconds=60.0).run_forever()
```

Set `stale_after_seconds` above your slowest single operation. Too low and two
workers pick up the same run, which is safe but doubles that run's model spend.

## Guardrails

Argument validation runs by default: the tool has to be registered, its
arguments have to match the declared schema, and numbers stay within any caps
you configure. A failure is returned to the model to correct rather than ending
the run.

Prompt-injection pattern matching is a separate layer, off unless asked for,
because the regexes have a substantial false-positive rate against ordinary tool
output. Profiles are `off`, `validation` (the default), `lenient`, `standard`
and `strict`.

```python
runtime = Runtime(store=..., llm=..., guardrail_profile="standard")
```

[`docs/THREAT_MODEL.md`](https://github.com/therajtiwari/durable-agents/blob/develop/docs/THREAT_MODEL.md) has the measured attack-success
and false-positive rates for each profile.

## HTTP API

```bash
pip install durable-agents[api]
```

```python
from durable_agents.api.app import create_app

app = create_app(store, default_max_steps=15)
```

| Endpoint | Description |
|---|---|
| `POST /runs` | Record a new run and return its id. Body: `{"goal": "..."}` |
| `GET /runs/{id}` | Status, pending approval, final answer, totals |
| `GET /approvals` | Runs currently waiting on a human |
| `POST /runs/{id}/approve` | Approve a parked run. Body: `{"approver": "..."}` |
| `POST /runs/{id}/deny` | Reject it. Body: `{"approver": "...", "reason": "..."}` |

No endpoint executes a run. Run a `Worker` alongside the API.

## Bringing your own model

One method:

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

Two implementations ship. `ScriptedLLM` takes a fixed list of responses, or
exceptions to simulate a flaky provider. `OpenAICompatibleClient` talks to
anything speaking the OpenAI chat-completions format: OpenAI, Azure, Groq,
Together, OpenRouter, Ollama, vLLM.

```python
from durable_agents.llm.openai_compatible import OpenAICompatibleClient

llm = OpenAICompatibleClient(
    base_url="https://api.openai.com/v1",
    model="gpt-4o-mini",
    api_key=os.environ["OPENAI_API_KEY"],
)
```

Neither client retries. Retries and their budget belong to the orchestrator.

## Limitations

- Postgres or in-memory only. No SQLite, MySQL or Redis.
- Recovery is poll-based. Nothing in the log records that a live process holds a
  run, so `Worker` infers it from silence. No leases, no distributed scheduler.
- Rebuilding state is O(events). No snapshotting, so a long run gets slower to
  resume.
- Tool calls within one model turn run sequentially, not concurrently.
- Your agent is built around `Runtime` rather than wrapping a loop you already
  have.

Event fields are only ever added, always with a default, and the meaning of an
existing field does not change. If that ever has to happen, `schema_version` is
added in the same release and its absence means version 1.

## Development

```bash
git clone https://github.com/therajtiwari/durable-agents && cd durable-agents
uv sync

uv run pytest tests/unit                           # no network, no database
uv run python examples/quickstart.py               # offline, in-memory
```

The integration and chaos suites need Docker and Postgres, as do the remaining
examples:

```bash
docker compose up -d
uv run durable-agents init-db

uv run pytest                                      # everything, ~70s
uv run python examples/offboarding_agent.py        # approval, retry, exactly-once
uv run python examples/crash_resume_demo.py        # kill it, run it again
uv run durable-agents replay <run_id>              # full trace of any run
```

Tests that hit a real provider are excluded by default. `pytest -m live
tests/live` opts in and skips if `LLM_API_KEY` is unset.

On Windows, consoles default to a legacy codepage and raise
`UnicodeEncodeError` on non-ASCII output. The CLI handles this; in your own
scripts use `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`.

## Docs

- [`docs/SPEC.md`](https://github.com/therajtiwari/durable-agents/blob/develop/docs/SPEC.md) — architecture and component reference
- [`docs/THREAT_MODEL.md`](https://github.com/therajtiwari/durable-agents/blob/develop/docs/THREAT_MODEL.md) — guardrail threat model and measurements
- [`DECISIONS.md`](https://github.com/therajtiwari/durable-agents/blob/develop/DECISIONS.md) — design decisions and rejected alternatives

## License

MIT
