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

---

## Iteration 2 — Postgres running, dependencies installed, events.py written (Week 1, Day 1 continued)

### Postgres is live, schema applied

`docker-compose.yml` now runs two containers:
- `postgres` — `postgres:16`, credentials `durable_agents`/`durable_agents`/`durable_agents`
  (user/password/db, all the same string — fine for local-only, never do this
  anywhere real), a named volume `pgdata` so data survives a restart, and a
  healthcheck (`pg_isready`) so anything waiting on it can tell "container
  started" apart from "actually accepting connections" — those are different
  moments and conflating them causes flaky startup races.
- `adminer` — a single-container web UI at `localhost:8080` for browsing the
  database visually (login: server `postgres`, the other three credentials
  above). Added because the user is new to Postgres tooling and wanted to
  *see* the table, not just trust `\d events` output. `postgres` as the
  "server" field works because Compose puts both containers on one private
  network with DNS entries named after each service — `adminer` reaches
  `postgres` by that service name, not `localhost`.

`db/migrations/001_events_table.sql` is applied to the running container.
One thing was corrected from the literal spec text: the migration originally
had two indexes, `idx_events_run_seq ON events (run_id, seq)` and a partial
index on `type`. The first one was dropped — `PRIMARY KEY (run_id, seq)`
already creates an identical unique B-tree index automatically, so the
explicit one was a byte-for-byte duplicate costing extra disk and slower
writes for zero query benefit. This is the first instance of a rule now
carried in memory: don't transcribe the spec, verify it. The remaining
partial index (`idx_events_type ... WHERE type IN ('ApprovalRequested',
'RunCompleted', 'RunFailed')`) earns its keep — it's a Postgres-specific
feature that indexes only rows matching the condition, used for the
cross-run queries in spec section 14 (e.g. "list every run currently
awaiting approval") where the caller doesn't know a `run_id` in advance.

### Dependency tooling: uv installed, .venv created, pyproject.toml written

`uv` wasn't on this machine at all — installed via the official Windows
installer script to `C:\Users\thera\.local\bin`. `pyproject.toml` was then
written with:
- `[build-system]` → `hatchling` (chosen over `uv`'s own newer `uv_build`
  backend — `hatchling` is the more battle-tested ecosystem default,
  relevant since this package is meant to publish to PyPI in week 6)
- Runtime deps: `pydantic>=2`, `asyncpg`
- Dev deps under `[dependency-groups]` (PEP 735, not the older
  `[project.optional-dependencies]` — that one is for user-facing extras
  like `durable-agents[postgres]`, not internal dev tooling): `pytest`,
  `pytest-asyncio`, `testcontainers[postgres]`, `mypy`
- `[tool.pytest.ini_options] asyncio_mode = "strict"` — every async test
  needs an explicit `@pytest.mark.asyncio` marker rather than pytest
  guessing; chosen because async/await is new territory here and implicit
  behavior would hide more than it'd save
- `[tool.mypy] strict = true`, matching the stack requirement in `CLAUDE.md`

`uv sync` then created `.venv` (a project-local Python environment — nothing
installed here is visible to any other project on the machine) and resolved
29 packages total (12 explicitly requested, the rest transitive — e.g.
`pydantic` needs `pydantic-core`, `testcontainers` needs `docker`) into
`uv.lock`, a generated, machine-written snapshot of exact versions. `uv.lock`
is committed to git; `.venv` is not — the lockfile is what makes the
install reproducible on another machine, the venv is just where those
packages physically sit on disk right now and can be recreated any time
with `uv sync`.

`.gitignore` was also filled in this iteration (it existed as an empty file
from Iteration 1's scaffolding and had been missed) — `.venv/`,
`__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, `.env`.

### events.py — the first real application code

Written after a structured back-and-forth on every field for all 13 event
types (see spec section 7 for the type list). Four genuine design decisions
were made, each with a rejected alternative:

1. **Nested Pydantic model, not a raw dict, for `tool_calls`.** Added a
   `ToolCallInvocation` class (`id`, `name`, `arguments`) instead of typing
   `LLMCallCompleted.tool_calls` as `list[dict]`. Rejected the plain-dict
   route because it gives up `mypy`'s ability to catch a typo'd key at the
   point a `ToolCallInvocation` is built or read, in exchange for a little
   less code.
2. **Closed `Literal[...]` types, not open `str`, for small fixed-value
   fields** — `GuardrailTriggered.layer`, `GuardrailTriggered.action`,
   `RunFailed.reason`. Pydantic now rejects a bad value at construction
   time instead of silently accepting a typo like `"Guardrial"`.
3. **`GuardrailTriggered.detail` stayed `dict[str, Any]`, not a fixed
   model.** The spec's own worked examples show genuinely different shapes
   per rule — a PII hit carries `replacements`, an injection match carries
   `matched`/`confidence`. A single fixed schema would mean most fields are
   null most of the time; a flexible dict matches what's actually true here.
4. **`ApprovalGranted` dropped the spec's literal "timestamp" field.**
   Every event already inherits `created_at` from `BaseEvent` — a second
   field recording the same moment would be redundant, since nothing in
   this project distinguishes "when the human clicked approve" from "when
   we recorded it" (an offline-approval system might need that gap; this
   one doesn't).

Also filled a spec gap rather than copying it: the table in section 7 lists
`ToolCallFailed`'s payload as just `error`, but that's not enough to
reconcile a dangling `ToolCallRequested` on resume without knowing *which*
tool call failed — so `ToolCallFailed` got `step`, `tool`, `arguments`,
`idempotency_key`, `attempt` added alongside `error`, mirroring
`ToolCallRequested`'s shape minus the result.

Verified, not just written: `mypy --strict` passes clean, and a manual
round-trip through `TypeAdapter(Event).validate_python(...)` confirmed the
discriminated union correctly resolves a `type: "ToolCallRequested"` payload
into a real `ToolCallRequested` instance, and rejects an unknown `type`
value with a `ValidationError`.

**Note on process:** this file is one of the four the project owner
committed to writing themselves (see `CLAUDE.md`'s do-not-implement list,
and spec section 20's "these five are your interview surface" reasoning).
They explicitly asked for it to be written for them anyway, after being
shown that tradeoff directly and choosing to override it. Worth being
aware of when reading this file later — it wasn't hand-written the way the
project's own stated plan intended.

### What's still not here

`storage/protocol.py` (the `EventStore` interface) is next — it was
blocked until `events.py` existed, since its method signatures need the
`Event` type to reference. `state.py`, `orchestrator.py`, and
`guardrails/decisions.py` are all still empty and still on the user's own
list to write.

---

## Iteration 3 — EventStore interface + Postgres implementation, verified live (Week 1, Day 3)

### storage/protocol.py — EventStore as an ABC, not a Protocol

Spec section 6 writes `EventStore` as a `Protocol` (structural typing —
any class with matching method names satisfies it, no explicit inheritance
needed). User chose an `ABC` instead (`abstractmethod`, explicit
`class PostgresEventStore(EventStore)` inheritance) — closer to a Java
interface, and you get a clear error if a method is missing rather than a
silent mypy-only check. Both achieve the same goal here; this was a
deliberate override of the spec's literal choice, not a mistake.

`ConcurrencyConflict` (raised by `append` when `expected_seq` is already
taken) lives in the same file as the interface that documents raising it —
simplest option while it's the only custom exception in the codebase.

### storage/postgres.py — the real implementation

Two decisions made before writing it:

1. **Constructor takes an existing `asyncpg.Pool`, plus a `connect(dsn)`
   classmethod for convenience.** Not "owns a DSN and builds its own pool
   internally." This means a test can inject any pool (e.g. one pointed at
   a `testcontainers` throwaway database) without the store needing to
   know or care where the pool came from — the store's only job is turning
   `Event` objects into rows and back, not connection management.
2. **The `payload` JSONB column holds only domain fields** — `seq`,
   `created_at`, and `type` are stripped out before storing, since those
   three already exist as real table columns. On read, they're merged back
   in from the row before reconstructing the `Event` via
   `TypeAdapter(Event).validate_python(...)`. This matches the spec's own
   worked example payloads in section 15 exactly (they never show `seq` or
   `type` as JSON keys) and avoids the same fact living in two places that
   could disagree if a future write path had a bug.

`asyncpg` has no `jsonb` codec by default — asyncpg returns/accepts that
column as a plain string unless you register one. Handled here with plain
`json.dumps`/`json.loads` on the payload only, rather than a connection-level
codec — more explicit and visible to read, at the cost of two extra lines
per method, judged worth it since hidden per-connection configuration is
exactly the kind of "magic" that's hard to debug when new to a language.

The concurrency check needs no application-level logic at all: `append`
just tries the `INSERT` and catches `asyncpg.UniqueViolationError`,
re-raising it as `ConcurrencyConflict`. Postgres's own primary key
constraint *is* the concurrency control — this is the same idea called out
in spec section 6, now visible directly in code instead of just prose.

**A gap mypy caught immediately:** `asyncpg` ships no type stubs at all, so
`mypy --strict` couldn't see inside `asyncpg.Pool`/`Record`/exceptions —
anything touching them was an invisible hole in type coverage. Added
`asyncpg-stubs` (community-maintained) as a new dev dependency rather than
silencing the whole module via a mypy override, so a typo'd method name on
`Pool` still gets caught before runtime.

### Verified against a real, running Postgres — not just mypy

Ran a manual script (not yet a committed pytest file) that: appended a
`RunStarted`, read it back and asserted full equality with the original
object, attempted the same `(run_id, seq)` a second time and confirmed
`ConcurrencyConflict` actually fires, and confirmed `read_since` correctly
returns nothing when there's nothing newer. All four passed.

**Environment issue hit and fixed along the way:** a native PostgreSQL 18
installed directly on Windows (Windows service `postgresql-x64-18`) was
also bound to port 5432, winning the bind over Docker's port-forward for
the container. Every connection attempt from a Windows process (including
the smoke-test script) to `localhost:5432` was silently reaching that
native install instead of the Docker container — different credentials,
hence an auth failure that had nothing to do with the code. `docker exec`
based checks didn't show this, because those talk to the container
directly, bypassing the host port. Fixed by stopping the native Windows
service (not uninstalling — easy to restart if something else on the
machine needs it). Worth remembering if Postgres connection issues show up
again on this machine: check `Get-NetTCPConnection -LocalPort 5432` for a
second listener before assuming the container or the code is at fault.

### What's still not here (superseded — see Iteration 4)

Formal pytest test files for any of this (the manual smoke script above
isn't committed anywhere) — the four Week 1 unit/integration tests from
spec section 16 (append/read round-trip, concurrency conflict, determinism,
every-prefix-valid) are still to be written. `state.py` (`rebuild_state`)
is still empty and still on the user's own list.

---

## Iteration 4 — state.py, and Week 1's tests all passing (Week 1, Day 3 continued)

### state.py — rebuild_state, and the types RunState needed that the spec didn't fully specify

`RunStatus`, `RunState`, `Message`, `InFlightOp`, `PendingApproval`,
`GuardrailHit`, `apply()`, and `rebuild_state()`. Also on the user's own
"write yourself" list — written by Claude after the same override pattern
as `events.py`: the conflict with `CLAUDE.md` was named explicitly, a
middle-ground option was offered, and the user chose full override anyway.

One structural fix to the spec here, not just a style choice: spec section
8's `RunState` includes a `run_id` field, but `rebuild_state(events: list[Event])`
never receives a `run_id` as an argument, and `Event` objects don't carry
`run_id` either (established in Iteration 3 — it's a database column, not
part of the log's payload). Those two facts can't coexist. Fixed by
dropping `run_id` from `RunState` entirely — whoever calls `rebuild_state`
already has the `run_id`, since they had to pass it to `EventStore.read()`
to get the events in the first place. Documented directly in `RunState`'s
docstring so this isn't mistaken for an oversight later.

`Message`, `InFlightOp`, `PendingApproval`, and `GuardrailHit` aren't in
the spec's code snippet at all — the spec just names them as types
`RunState` needs (`in_flight: InFlightOp | None`, etc.) without defining
them. All four were designed from scratch, worth knowing since they're not
literal spec transcriptions:
- `Message` is deliberately minimal and provider-agnostic (`role`,
  `content`, `tool_calls`, `tool_name`) — no LLM client exists yet (that's
  week 2), so this shape is a placeholder likely to change once there's a
  real provider API it needs to match.
- `InFlightOp` carries enough to reconcile on resume: `kind` (`"llm"` or
  `"tool"`), the originating `seq`/`step`, and for tool ops specifically
  `tool`/`arguments`/`idempotency_key` — the exact fields spec section 9's
  reconciliation logic needs to decide whether to redo an LLM call (safe,
  cheap) or replay a tool call by its idempotency key (the only thing
  standing between a resumed run and a double refund).
- `PendingApproval` and `GuardrailHit` are thin — just enough fields to
  answer "what's blocking this run" and "what got flagged," matching what
  spec sections 13/14's queries actually need.

`apply()` uses Python's `match`/`case` structural pattern matching
(added in 3.10) — one `case ClassName():` per event type, closest Python
idiom to a Java `switch` over a sealed interface's implementations. The
final `case _: assert_never(event)` is what makes this provably exhaustive:
because `Event` is a *closed* union of exactly 13 types, `mypy --strict`
only accepts `assert_never` at that branch if every other case has already
narrowed the type away completely. Add a 14th event type to `events.py`
without adding its case here, and `mypy` fails at that exact line instead
of the gap surfacing as a runtime bug later.

Two design choices worth knowing for each event's handler:
- `LLMCallFailed`/`ToolCallFailed` leave `state` completely unchanged.
  The dangling `Requested` stays `in_flight` — per spec section 8's own
  rule, only a `Completed` clears it. A failure is just a recorded fact;
  it doesn't resolve anything by itself.
- `RunCompleted` overwrites `total_tokens`/`total_cost_usd`/`step` with
  its own authoritative values rather than trusting the running totals
  accumulated from individual `LLMCallCompleted` events — matching spec's
  own worked example where `RunCompleted`'s payload is the final tally.

Verified against every property spec section 8 calls out, using a
hand-built event sequence: determinism (`rebuild_state(events) ==
rebuild_state(events)`), a dangling `ToolCallRequested` producing a
non-null `in_flight`, a matching `ToolCallCompleted` clearing it, and
every prefix of the sequence (including the empty list) rebuilding without
error.

### Formal tests written and passing — Week 1's actual "done when"

Unlike the four core files, test files are explicitly fine to write
directly (spec section 20 lists "test scaffolding" under delegate-freely).

`tests/unit/test_state.py` — 7 tests, no database, sub-second: the four
properties above plus a few extras (empty log gives `"not_started"`, an
LLM-side dangling request too, `RunCompleted`'s totals actually win).

`tests/integration/test_postgres_store.py` + `tests/integration/conftest.py`
— 4 tests against a **real, disposable Postgres** spun up per test session
via `testcontainers`, not the long-running dev container from
`docker-compose.yml`. Covers exactly the two integration-level checks spec
section 16 names: append/read round-trip, and `ConcurrencyConflict` firing
on a genuine duplicate `(run_id, seq)` — plus a check that `read_since`
correctly excludes the event at the boundary seq, and that two different
`run_id`s never see each other's events.

**Two real gotchas hit and fixed while getting these green, both worth
remembering:**

1. **asyncpg connections are bound to the event loop they were created
   under.** First version of `conftest.py` made the Postgres connection
   pool a *session-scoped* fixture (created once, reused across all
   tests) — but `pytest-asyncio` in strict mode gives each test function
   its own event loop by default. The pool's connections, created under
   the session's throwaway setup loop, were then unusable from any actual
   test's loop: `RuntimeError: got Future ... attached to a different
   loop`. Fixed by keeping only the **Docker container itself**
   session-scoped (slow to start, worth reusing) while giving each test
   function its own fresh `asyncpg` pool, created and closed within that
   test's own loop. The one-time schema migration is applied via a
   throwaway `asyncio.run()` call that starts and closes its own loop
   immediately, touching asyncpg exactly once outside any pytest-asyncio
   machinery — so there's never a connection to accidentally reuse across
   loops.
2. **mypy treats your own package as untyped once it's imported rather
   than analyzed by path.** Running `mypy src/durable_agents/state.py`
   directly worked fine; running `mypy tests/` (where test files `import
   durable_agents...` normally) produced `Skipping analyzing
   "durable_agents.events": ... missing library stubs or py.typed marker`
   for every internal import. Per PEP 561, a package needs an empty
   `py.typed` marker file to tell type checkers "trust my inline
   annotations" once it's resolved as an installed dependency rather than
   read directly from a file path. Added
   `src/durable_agents/py.typed` (empty file) — fixed immediately, no
   reinstall needed since the project is installed in editable mode.

**Full verification, all green:** `mypy --strict` clean across all 27
source files (`src/` + `tests/`), 11/11 tests passing (7 unit in ~0.25s, 4
integration in ~6s including spinning up and tearing down a real
container).

### Week 1 status

Everything in spec section 18's Week 1 scope now exists and is verified:
Postgres schema, `EventStore` (interface + Postgres implementation),
`rebuild_state`, and tests proving the properties that matter. `state.py`,
`orchestrator.py`, and `guardrails/decisions.py` remain the user's own
files — `state.py` is now written (by exception, see above);
`orchestrator.py` and `guardrails/decisions.py` are Week 2+ work and still
genuinely untouched.
