# Build Log

Detailed build log. One section per iteration where concrete work landed —
skipped for planning-only or discussion-only sessions. Written for someone
who has never opened this repo: what exists, why, and how it fits together.

Companion to `PROGRESS.md` (terse status) and `DECISIONS.md` (why a specific
choice was made over alternatives). This file is the "what is going on in
the code" narrative.

---

## Iteration 1 — Repo scaffolding (Week 1, Day 1)

### What exists right now

Nothing runs yet. Every `.py` file created this iteration is empty. What
was built is the **shape** of the project — directories and file names —
so that later code has an obvious home instead of being figured out
file-by-file as it's written.

### The layout, and why each part exists

```
src/durable_agents/
    events.py          empty — will hold the event type definitions
    state.py            empty — will hold rebuild_state()
    orchestrator.py      empty — will hold the main agent loop
    storage/             empty — will hold the event store (Postgres-backed)
    llm/                  empty — will hold the LLM client wrapper
    tools/                empty — will hold the tool registry + refund tools
    guardrails/           empty — will hold the four-layer safety checks
    api/                  empty — will hold the FastAPI HTTP layer
    cli.py               empty — will hold the command-line entry points
tests/
    unit/ integration/ chaos/ guardrails/ property/ live/   all empty
db/migrations/           empty — will hold the SQL schema for the events table
```

**Why `src/durable_agents/` and not just `durable_agents/` at the repo
root:** this is the "src layout," a Python packaging convention. Without
it, Python can accidentally import your package straight from the source
folder during testing instead of the actual installed package — bugs that
only show up after `pip install`. Putting a `src/` directory in between
forces every import to go through the real installed package, so what you
test is what you'd actually ship. This matters here because the project
gets published to PyPI in week 6 (see `docs/SPEC.md` section 17).

**Why events/state/orchestrator are separate top-level files, not folders:**
these three are the core of the whole system — the event schema, the pure
function that turns events into current state, and the loop that ties
everything together. They're also the four files (`orchestrator.py`,
`events.py`, `state.py`, plus `guardrails/decisions.py`) the project owner
is writing by hand rather than delegating, because they're the parts you
need to be able to explain in an interview. See the "do not implement"
list in `CLAUDE.md`.

**Why `storage/`, `llm/`, `tools/`, `guardrails/`, `api/` are folders, not
single files:** each of these has more than one implementation sharing one
interface:
- `storage/` will hold `protocol.py` (the `EventStore` interface — just
  method signatures, no logic) and `postgres.py` (the real implementation
  against Postgres). Splitting these means swapping Postgres for something
  else later (the spec mentions Kafka) only touches this one folder.
- `llm/` will hold one `protocol.py` and three implementations: a real
  Anthropic client, a `ScriptedLLM` for tests (returns a canned list of
  responses, no network call), and a `ReplayLLM` (replays a previously
  recorded run). Tests use the fake ones so they're fast, free, and don't
  depend on a flaky network call.
- `tools/` splits the generic registration mechanism (`registry.py` — the
  `@tool` decorator) from the actual business logic (`refund_tools.py` —
  the three tools: look up an order, check refund policy, issue a refund).
- `guardrails/` gets one file per safety layer (input scanning, tool-result
  scanning, output validation, run-level checks) plus `decisions.py` for
  the logic that decides what action to take when a check fires. Layers
  live in separate files so it's easy to verify the *order* they run in —
  getting that order wrong (e.g., checking for PII after a tool result has
  already been sent to the model) is a real security bug, not a style
  issue.

**Why `api/` is separate from the core package:** FastAPI is how a human
or a script talks to the system over HTTP. The orchestrator and everything
underneath it should never require a running web server to be tested —
keeping `api/` as its own folder makes that boundary visible.

**Why `tests/` has six subfolders instead of one flat folder:** each
folder is a different *kind* of test with a different cost and purpose:
- `unit/` — pure functions only (like `rebuild_state`), no database, no
  network, runs in milliseconds.
- `integration/` — talks to a real (test) Postgres instance and a fake
  LLM, checks components work together.
- `chaos/` — deliberately kills the process mid-run (`SIGKILL`) and checks
  it resumes correctly without duplicating side effects. This is the suite
  that proves the whole "durable" claim, not just asserts it.
- `guardrails/` — runs a corpus of attack and benign inputs, measures both
  how many attacks got through and how many legitimate requests got
  wrongly blocked.
- `property/` — uses Hypothesis to generate random event sequences and
  check invariants hold no matter what.
- `live/` — the only tests that hit the real Anthropic API. Costs real
  money, so these are run manually, not on every commit.

**Why `db/migrations/` holds raw SQL, not an ORM migration tool:** the
entire schema is one table (`events` — see `docs/SPEC.md` section 6). A
migration framework like Alembic would be overhead for something this
small; a plain `.sql` file is easier to read top to bottom.

### What is deliberately *not* here yet

- No dependencies are declared in `pyproject.toml` — it's empty.
- No `uv.lock` — that file is generated by the `uv` tool once real
  dependencies exist; hand-writing one would be meaningless.
- No actual event types, no actual database connection code, no actual
  agent loop. Week 1's real work (Postgres schema, event types, the event
  store, `rebuild_state`) hasn't started — this iteration was scaffolding
  only, explicitly agreed as a separate step before any code gets written.

### How this connects to what comes next

The very next concrete change will be inside `db/migrations/` (the SQL
`CREATE TABLE events` statement) and `pyproject.toml` (declaring `asyncpg`,
Pydantic, etc. as dependencies) plus a `docker-compose.yml` that runs
Postgres locally. After that, `events.py` gets real Pydantic event classes,
and `state.py` gets the real `rebuild_state` fold. Each of those will get
its own section in this file once it lands.
