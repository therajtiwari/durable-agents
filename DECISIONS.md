# Decisions

The forks that had a real alternative, and why this side was taken.
`docs/BUILD_LOG.md` is the chronological narrative; this is the index of
the choices worth defending. Where a decision has a known cost, the cost
is stated rather than omitted.

---

## Storage and consistency

**The `(run_id, seq)` primary key *is* the concurrency control.**
Rejected: advisory locks, a `runs.version` column, `SELECT … FOR
UPDATE`. Two workers racing on the same run cannot both insert seq 7 —
the database rejects one, that worker re-reads and re-decides. No lock
to acquire, no lock to leak, no lease to expire. Proven under real
contention in `tests/integration/test_concurrent_workers.py` (two
orchestrators, two connection pools, one run).

**`ConcurrencyConflict` is swallowed inside `_append`, not handled by
callers.** Rejected: propagating it and having each call site recover.
Every call site in `run()`'s loop already falls through to the top,
which re-reads events and rebuilds state — so the recovery was already
written; it just needed the exception to stop killing the process.

**Rejected a second index on `(run_id, seq)`** that the spec suggested:
the primary key already creates one. A duplicate would cost write
throughput for no query benefit.

**`EventStore` is an ABC, not a `Protocol`** (the spec's shape).
Nominal typing was wanted here: this is a public extension point where
"did you mean to implement this?" should be an explicit, checkable yes.
`RefundBackend`, a private internal seam, *is* a `Protocol` — the
distinction is deliberate.

**`InMemoryEventStore` ships in the package**, not just in tests.
A library that can't be tried without Docker and a migration step loses
most evaluators at step one.

**The schema ships inside the package** (`storage/schema.sql`, read via
`importlib.resources`), not only in `db/migrations/`. Rejected: keeping
SQL in the repo and documenting it. A wheel containing no way to create
its own table isn't installable in any practical sense.

---

## State

**`rebuild_state()` is pure — no I/O, no clock, no randomness.**
This is what makes crash recovery and ordinary operation *the same code
path* rather than two implementations that must be kept in agreement.
Everything impure lives in the orchestrator.

**`RunState` has no `run_id` field.** Whoever calls `rebuild_state` had
to pass a `run_id` to `EventStore.read()` to obtain the events; carrying
it again would be a second source of truth for one fact.

**Approval resumption uses `RunState.approved_step`.** Rejected: having
the orchestrator walk the event log backwards to find the
`ApprovalRequested` a grant resolves. Every other component derives
facts from state exactly once, via the fold; making the orchestrator do
its own event-log archaeology would break that discipline for one
feature.

**Retry budgets live in the event log** (`InFlightOp.attempts`), not in
an orchestrator variable. Rejected: an in-process retry loop. A local
counter resets to zero on every crash, so a flapping provider plus a
crash-loop retries forever. In the log, a resumed process inherits
exactly what a dead one spent.

---

## Failure handling

**A tool that raises is retried in place with the *same* idempotency
key**, and only surfaced to the model once the budget is spent
(`ToolCallFailed.final_attempt`). Rejected: surfacing the error
immediately. That was the original behaviour and it hid a real
exactly-once hole — the model would issue a *fresh* tool call at a new
seq, hence a **new** idempotency key, which no backend would
deduplicate. A payments call that timed out after succeeding would be
charged twice.

**Every exception is treated as retryable.** Distinguishing a transient
429 from a permanent 400 needs provider-specific knowledge this layer
deliberately doesn't have. A bounded budget makes retrying a
non-transient error merely wasteful rather than unsafe.

**`LLMCallFailed` leaves the operation in flight; `ToolCallFailed`
(final) clears it.** An LLM failure resolves nothing — the same call
gets attempted again. A terminal tool failure is a fact the model needs
to see, so it becomes a `tool`-role message and the model chooses what
to do next. Getting this wrong once caused a real infinite loop on a
hallucinated tool name, caught by an orchestrator test that `state.py`'s
own unit tests couldn't have caught.

---

## Tools

**Idempotency key = `sha256(run_id + seq + tool + canonical_json(args))`.**
Deterministic: the same logical step yields the same key no matter how
many times the process restarts. The spec's `idempotency="args"`
decorator parameter was **dropped** — its own formula never used the
value.

**A parameter literally named `idempotency_key` is excluded from the
LLM-facing schema and injected by the runtime.** Rejected: a separate
registration flag. The convention is self-documenting at the call site
and impossible to forget.

**`Tool` keeps `args_model`,** the Pydantic model it already built to
derive the JSON schema. Rejected: adding a `jsonschema` dependency for
L3 validation. The model was being constructed and thrown away.

---

## Guardrails

**Detection functions are plain functions taking their patterns as
data.** Rejected: the spec's `class InputGuard: async def check(...)`.
`EventStore` and `LLMClient` are ABCs because there are genuinely
swappable *implementations*; here there is one algorithm and swappable
*data*.

**PII patterns are pluggable, and the core defaults are
locale-neutral** (email, Luhn-checked card, IBAN, international phone).
Rejected: shipping India-specific patterns (PAN, Aadhaar, IFSC) as core
defaults, which an earlier draft did by copying the spec's worked
example. A library published to the world can't assume one country's ID
formats. Country-specific patterns are the consumer's configuration.

**Policy caps are configuration, not constants,** for the same reason —
`₹1,00,000` is the demo's number, not a claim about anyone's business.

**Deterministic violations always `BLOCK`, regardless of profile.**
Only confidence-based injection matches vary by strictness. A schema
violation or an exceeded numeric cap isn't a probabilistic guess that a
stricter setting should be more suspicious of.

**Redaction and delimiting happen at the LLM boundary
(`_sanitize_for_llm`), recomputed on every call — never by mutating
stored events.** Rejected: redacting once and persisting. That would
either corrupt the audit trail or protect only the turn it happened on
(a tool result from step 1 still needs delimiting when the history is
resent at step 5).

**The measured 20% false-positive rate was reported, not tuned away.**
One case (`"disregard the previous refund amount typo"`) is genuinely
ambiguous phrasing; weakening the pattern to pass it would let a real
`"disregard the previous instructions"` attack through. See
`docs/THREAT_MODEL.md`.

---

## API surface

**`Runtime` supplies tools once, at construction — not per-run.**
The spec's example passes `tools=[...]` to `start()`. Rejected because
nothing in the event log records which tools were registered, so a
resumed run could silently execute against a different tool set than the
one that produced the events being replayed.

**`start()` records *and* executes; `create()` records only.**
Rejected: a single `start()` that only enqueues. With no worker or
sweeper built yet, that would make the README's headline example do
nothing visible.

**The API layer appends `ApprovalGranted`/`ApprovalDenied` directly.**
This required narrowing an invariant that said "only the orchestrator
appends events" — written to stop *leaf components* (LLM, tools,
guardrails) from faking outcomes past the orchestrator, and never meant
to cover a human decision arriving over HTTP. The wording was corrected
rather than worked around with a pass-through method.

**`fastapi` is an optional extra, not a hard dependency.** The runtime
never imports it; only `durable_agents.api.app` does.

**`POST /runs` records only — it does not execute.** Rejected: blocking
the HTTP request until the run completes or parks (mirroring
`Runtime.start()` instead of `create()`). An agent run can take
minutes; holding an HTTP connection open that long is fragile against
timeouts, load balancers, and client retries. It also would have
required the API layer to own an `LLMClient` and tool registry — a real
architecture change from today's store-only `create_app(store)` — for
a shape that contradicts the rest of the surface anyway, since
`approve`/`deny` already only record decisions and leave execution to
whatever process calls `resume()`. Defaults (`model`, `max_steps`,
`max_cost_usd`, `guardrail_profile`) are `create_app()` keyword
arguments rather than hardcoded, so a deployment configures its own
policy without touching the endpoint's code.

**`system_prompt_hash` is derived from `system_prompt`, not supplied.**
Rejected: two independent fields. They could disagree, and for five
weeks the log recorded a hash of a prompt that didn't exist anywhere.
The validator only fills a hash that's absent, so events written before
the field existed keep the hash they were stored with.

**`durable-agents demo` and `durable-agents resume` are two different
commands, not one.** They used to be the same command (`start`/`resume`
both called one shared `_start_or_resume`) which *always* wired up a
hardcoded scripted refund conversation against fake refund tools —
discovered as a real point of confusion when a user created a run with
an arbitrary goal via `POST /runs` and then ran `resume` on it,
expecting their own goal to run, and got refund behavior instead. The
CLI had silently ignored the run's own recorded goal and replayed its
one fixed demo script regardless of which `run_id` it was given.

Split: `demo` keeps the exact old zero-setup behavior (fixed script,
fake tools, no API key, no network) for proving crash-resume works with
nothing configured. `resume` is now generic — a real `LLMClient`
(`OpenAICompatibleClient`, configured via `LLM_API_KEY`/`LLM_BASE_URL`/
`LLM_MODEL`, same convention as `tests/live`) and **no tools at all**,
since the CLI has no way to know what functions a specific deployment
wants wired up for an arbitrary run. Rejected: giving `resume` a way to
load tools from a config file or module path — real feature, more
surface area than this fix needed; a run that needs tools gets a real
script against `Runtime`/`Orchestrator` instead (already how every
`examples/live_*.py` script works).

---

## Provider client

**`OpenAICompatibleClient`, not `AnthropicClient`.** Spec section 10
names `AnthropicClient` as the reference implementation. Rejected:
shipping one vendor-specific SDK wrapper as the library's only real
client is the same shape of bias already caught and fixed in
guardrails — a "universal" library defaulting to one company's format.
The OpenAI chat-completions wire format is what most providers,
including most local/open-source model servers (Ollama, vLLM, Groq,
Together, OpenRouter, Azure OpenAI), actually speak — one
implementation covers more real usage than a client tied to one SDK.
`LLMClient` itself was always vendor-neutral (one abstract method); the
narrowness was only ever in which concrete implementation got shipped.

**`orchestrator._tool_schemas()`'s output was silently Anthropic-shaped**
(`input_schema`, Anthropic's own field name) until this was caught while
building the generic client. Renamed to the neutral `parameters` — the
orchestrator now hands every `LLMClient` implementation a
provider-agnostic shape, and translating that into a specific wire
format (OpenAI wraps it in `{"type": "function", "function": {...}}`,
Anthropic wants it flat under `input_schema`) is each client's own job,
matching what `LLMClient.call()`'s docstring already claimed but the
code didn't actually do.

**Cost is computed from configurable per-1k-token rates, not a
hardcoded price table.** Prices vary by model and change often;
hardcoding one vendor's numbers into a generic client would go stale
immediately and re-introduces the same bias problem in a different
shape. Defaults to `$0` if you don't configure rates.

**The client does not retry internally.** `Orchestrator` already
retries a failed LLM call with backoff, reading the attempt budget from
the event log (Iteration 24). A client-level retry would double the
backoff for no benefit and duplicate logic that already exists at the
layer meant to own it.

**~~The OpenAI-shaped `tool_call_id` is recovered by pairing, not
persisted.~~ Superseded — the id is now persisted.** The original
decision had the client pair each tool-role message with the preceding
assistant message's `tool_calls[0].id`, to avoid adding a field to
`ToolCallRequested`/`ToolCallCompleted` for what looked like a
client-side formatting detail. It carried the caveat "valid today
because the orchestrator only ever acts on one tool call per step;
would need revisiting if that ever changes."

That caveat treated a **limitation** as a stable premise. Acting on one
tool call per step was never a decision anyone made — it was inherited
from spec §15's worked example, where every step happens to have
exactly one call, and it silently discarded calls 2..n of any batch. So
the pairing hack was not a neat avoidance of schema churn; it was the
downstream symptom of a correctness bug. Both are fixed together in
Iteration 33: `tool_call_id` is a real field on
`ToolCallRequested`/`Completed`/`Failed`/`ApprovalRequested`, and the
client reads it directly. The positional pairing survives only as a
fallback for logs written before the field existed.

---

## Worker and recovery

**Staleness is inferred from the newest event, not from a lease.**
The hard problem: a run being actively worked by a live process looks
identical in the log to one whose worker died mid-operation — nothing
records "a worker is holding this". Rejected: a leases table where a
worker claims a run with an expiry. That would be a second source of
truth alongside the event log, need renewal and expiry handling, and
reintroduce exactly the lock-leaking failure modes the `(run_id, seq)`
primary key design was chosen to avoid.

Instead `find_resumable_runs` reads the *type* of the newest event:
`RunStarted`, `ApprovalGranted`, and `ApprovalDenied` prove no worker
can be mid-operation (nobody has begun it, or a human just acted), so
those are returned immediately — which is what lets a run created over
the API start right away instead of waiting out a threshold. Anything
else might mean a worker is inside an LLM call, a tool call, or a retry
backoff, so it waits for `stale_after_seconds` of silence.

The tradeoff is stated rather than hidden: set the threshold too low
and two workers race. That is *safe* — `(run_id, seq)` already makes
concurrent execution correct, proven in
`tests/integration/test_concurrent_workers.py` — but it duplicates
model spend for that run. Wasted money, never corruption.

**One `Worker` class, not a separate Worker and Sweeper.** Spec
describes them as two processes. They are the same mechanism — find a
run_id, call `resume()` — differing only in how long a run must be
quiet before it's considered abandoned, so that duration is a
parameter. Run one instance for both jobs, or two with different
thresholds for spec's split. A deliberate divergence, documented rather
than silently followed.

**The Worker ships in the library, not in `examples/`.** It takes a
`Runtime`, which already carries the consumer's tools and LLM client,
so nothing about it is demo-specific. This is the piece that turns
"durable" into "self-healing" — leaving every consumer to rewrite the
same polling loop would mean the project's most quotable property is
demonstrated but not actually shipped.

**A failing run is logged and skipped, not allowed to kill the loop.**
`poll_once` catches per-run exceptions so one poisoned run can't become
an outage for every other run; `run_forever` separately catches
failures of the polling query itself so the worker recovers when a
database comes back rather than needing a restart.

---

## Parallel tool calls

**A batch is executed one call at a time, not concurrently.** A model
may ask for several tools in one response; each gets its own
`ToolCallRequested`/`ToolCallCompleted` pair, issued in order, and the
model is not called again until every call in the batch has a result.
Rejected: `asyncio.gather` over the batch. Concurrency is an
optimisation, and it would mean several operations in flight at once —
turning crash recovery from "one dangling `Requested`" into a
multi-operation reconciliation, which is a materially harder problem
for a benefit no correctness property needs. Sequential execution keeps
recovery exactly as it was.

**Approval is granted per tool call, not per step.** `RunState` gained
`approved_tool_call_id`; the old `approved_step` remains only as the
fallback for runs parked before approvals recorded an id. Once a step
can carry several calls, a step-level grant would mean approving the
one call that needed a human silently released every other call in the
same batch — including a destructive one the human never saw.

**Every new field defaults to empty, so existing logs rebuild
unchanged.** A tool-role message with no `tool_call_id` answers the
first still-open call, which is exactly what a pre-change log meant by
position, since only one call was ever outstanding then. There is a
test that builds such a log by hand and proves the completed call is
not re-executed.

---

## Known open questions

Recorded rather than quietly settled:

- **Incremental adoption.** The current design asks a consumer to build
  their agent *around* this runtime. Wrapping an agent loop someone
  already has would likely matter more for real usage than anything
  else on the roadmap.
- **Cap checks precede the in-flight check** in the main loop, so a run
  can hit its step/cost cap while a tool call is genuinely dangling,
  leaving that side effect unresolved.
- **An L1 `ESCALATE` fails closed (`BLOCK`)** because there is no tool
  call in context to attach an `ApprovalRequested` to, and that event's
  schema requires one.
- **Whether guardrails belong in this package at all**, or as a
  separate optional one — durability and injection defense have
  different audiences, and pattern-based detection is the weakest part
  of the project.
