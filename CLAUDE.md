# CLAUDE.md

## Source of truth

`docs/SPEC.md` is the single reference for this project — architecture, every
component, storage format, guardrails design, testing strategy, and the
six-week build plan. Read it before proposing any design. If something here
conflicts with it, SPEC.md wins; flag the conflict instead of silently
picking one.

## Invariants — never violate these

- **The event log is append-only.** Never `UPDATE events SET ...`, never
  `DELETE FROM events`. A state change is always a new row with the next
  `seq`, never a mutation of an existing row.
- **`rebuild_state()` must stay pure.** No I/O, no wall-clock reads, no
  randomness, no network calls. Same events in, same state out, every time.
  If a change to this function needs any of those, the change is wrong.
- **Only the orchestrator appends events.** Leaf components (LLM client,
  tools, guardrails) do work and return results — they never touch the event
  store directly. This is what keeps them independently testable.
- **Event names are past tense.** `ToolCallCompleted`, never
  `CompleteToolCall`. Events are facts about what already happened, not
  commands.

## Do not implement unless explicitly asked

These files are the user's interview surface — they are learning by writing
them, not by reading generated code. Do not create, fill in, or rewrite
their contents unless the user explicitly asks in that turn:

- `src/durable_agents/events.py`
- `src/durable_agents/state.py`
- `src/durable_agents/orchestrator.py`
- `src/durable_agents/guardrails/decisions.py`

Scaffolding (empty files, directory layout, docstub headers) is fine.
Logic inside them is not, even if it looks obvious or small.

## Stack

- Python 3.12
- `uv` for packaging and dependency management
- `asyncpg` for Postgres access
- Pydantic v2 (discriminated unions for events)
- FastAPI for the HTTP API
- `pytest-asyncio` for async tests
- `mypy` strict

## Dependencies

Ask before adding any new dependency, including dev dependencies. State what
it's for and what the lighter-weight alternative would have been.

## Working rules

- Before writing any non-trivial code: explain the approach and at least one
  rejected alternative, then wait for the user to choose. They make the
  design decisions, not you.
- When asked "why", explain reasoning — do not respond by rewriting code.
- The user is an experienced Java/Spring backend developer, new to Python
  and new to GenAI. Call out Python-specific idioms and gotchas as they
  come up, especially around `asyncio` — their instincts from Java threads
  will be wrong there.
- Keep changes small: one concern per change, so every change is reviewable
  in full before it's committed.
