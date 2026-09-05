import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from durable_agents.state import RunState, rebuild_state
from durable_agents.storage.postgres import PostgresEventStore
from durable_agents.tools.registry import idempotency_key

# Configurable for the same reason scenario_runner.py already reads it:
# the suite spawns real separate processes that must all reach one
# database, so it cannot use a testcontainer, and where that database
# lives differs between a developer's machine, CI, and a container.
DSN = os.environ.get(
    "DATABASE_URL", "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"
)
RUNNER = str(Path(__file__).parent / "scenario_runner.py")

# Matches tests/chaos/scenario_runner.py's canonical scripted scenario
# exactly: lookup_order (seq 1-4), check_refund_policy (seq 5-8),
# issue_refund (seq 9-12), final answer (seq 13-15). 16 events total,
# seq 0 through 15.
ISSUE_REFUND_ARGS = {"order_id": "A-8891", "amount_inr": 3000, "reason": "damaged"}
ISSUE_REFUND_REQUEST_SEQ = 11
ISSUE_REFUND_COMPLETE_SEQ = 12
LAST_MEANINGFUL_KILL_SEQ = 14  # killing after 15 (RunCompleted) proves nothing


EXAMPLES = str(Path(__file__).resolve().parents[2] / "examples")


def _run_scenario(run_id: UUID, env_overrides: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    # PYTHONPATH, not pytest's pythonpath setting: the runner is a
    # genuinely separate process (that is the whole point of this suite),
    # so it inherits none of pytest's sys.path manipulation and has to be
    # told where the refund demo lives on its own.
    existing = os.environ.get("PYTHONPATH")
    return subprocess.run(
        [sys.executable, RUNNER, str(run_id)],
        env={
            **os.environ,
            "PYTHONPATH": f"{EXAMPLES}{os.pathsep}{existing}" if existing else EXAMPLES,
            **env_overrides,
        },
    )


async def _verify(run_id: UUID, key: str) -> tuple[RunState, int, bool]:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        store = PostgresEventStore(pool)
        events = await store.read(run_id)
        state = rebuild_state(events)
        row = await pool.fetchrow(
            "SELECT COUNT(*) AS n FROM refund_ledger WHERE idempotency_key = $1", key
        )
        assert row is not None
        completion = next(
            e for e in events if e.seq == ISSUE_REFUND_COMPLETE_SEQ and e.type == "ToolCallCompleted"
        )
        assert completion.type == "ToolCallCompleted"
        return state, int(row["n"]), completion.recovered
    finally:
        await pool.close()


@pytest.mark.parametrize("kill_after_seq", range(0, LAST_MEANINGFUL_KILL_SEQ + 1))
def test_resume_from_any_kill_point(kill_after_seq: int) -> None:
    """A real SIGKILL (TerminateProcess on this platform — see
    orchestrator.py) at every meaningful point in the canonical run.
    Resuming must always reach RunCompleted with exactly one refund ever
    created, regardless of which event was the last one durably recorded
    before the process died.
    """

    run_id = uuid4()

    killed = _run_scenario(run_id, {"CHAOS_KILL_AFTER_SEQ": str(kill_after_seq)})
    assert killed.returncode != 0, f"expected an abrupt kill, got returncode {killed.returncode}"

    resumed = _run_scenario(run_id, {})
    assert resumed.returncode == 0, "resume should complete the run cleanly"

    key = idempotency_key(run_id, ISSUE_REFUND_REQUEST_SEQ, "issue_refund", ISSUE_REFUND_ARGS)
    state, refund_count, _recovered = asyncio.run(_verify(run_id, key))

    assert state.status == "completed"
    assert refund_count == 1


# The parallel scenario: one model turn asking for three refunds, so the
# log runs RunStarted(0), LLMCallRequested(1), LLMCallCompleted(2), then
# a Requested/Completed pair per refund (3-8), then the closing turn
# (9-10) and RunCompleted(11). Killing after 11 proves nothing.
PARALLEL_LAST_MEANINGFUL_KILL_SEQ = 10


async def _verify_parallel(run_id: UUID) -> tuple[RunState, list[str], list[int]]:
    """Returns the final state, the idempotency key of every tool call
    the log actually requested, and how many ledger rows each produced.

    Keys are read back out of the log rather than recomputed from
    hardcoded seqs: the point of this test is that the batch survives a
    crash at any point, so which seq a given refund landed on is part of
    what's under test, not a constant to assert against.
    """

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        store = PostgresEventStore(pool)
        events = await store.read(run_id)
        keys = [e.idempotency_key for e in events if e.type == "ToolCallRequested"]
        counts = []
        for key in keys:
            row = await pool.fetchrow(
                "SELECT COUNT(*) AS n FROM refund_ledger WHERE idempotency_key = $1", key
            )
            assert row is not None
            counts.append(int(row["n"]))
        return rebuild_state(events), keys, counts
    finally:
        await pool.close()


@pytest.mark.parametrize("kill_after_seq", range(0, PARALLEL_LAST_MEANINGFUL_KILL_SEQ + 1))
def test_resume_mid_batch_from_any_kill_point(kill_after_seq: int) -> None:
    """A real kill at every point of a run whose model asked for three
    refunds in a single turn.

    Three separate side effects are now in play per model turn rather
    than one, so a crash can land between them. Resuming must produce
    exactly three refunds: not two (a dropped call) and not four (a
    duplicated one, which is the guarantee the idempotency key exists to
    give).
    """

    run_id = uuid4()

    killed = _run_scenario(
        run_id, {"CHAOS_KILL_AFTER_SEQ": str(kill_after_seq), "CHAOS_SCENARIO": "parallel"}
    )
    assert killed.returncode != 0, f"expected an abrupt kill, got returncode {killed.returncode}"

    resumed = _run_scenario(run_id, {"CHAOS_SCENARIO": "parallel"})
    assert resumed.returncode == 0, "resume should complete the run cleanly"

    state, keys, counts = asyncio.run(_verify_parallel(run_id))

    assert state.status == "completed"
    assert len(keys) == 3, f"all three refunds must be requested, got {len(keys)}"
    assert len(set(keys)) == 3, "each refund needs its own idempotency key"
    assert counts == [1, 1, 1], f"exactly one ledger row per refund, got {counts}"


def test_kill_after_side_effect_before_completion_stays_exactly_once() -> None:
    """The nastiest gap of all: the tool's side effect already ran, but
    nothing recorded that fact yet. A resumed
    reconcile() calls issue_refund again — only the idempotency key,
    checked by the backend itself, stops that from becoming a second
    real refund.
    """

    run_id = uuid4()

    killed = _run_scenario(
        run_id, {"CHAOS_KILL_AFTER_TOOL_EXECUTION_SEQ": str(ISSUE_REFUND_COMPLETE_SEQ)}
    )
    assert killed.returncode != 0

    resumed = _run_scenario(run_id, {})
    assert resumed.returncode == 0

    key = idempotency_key(run_id, ISSUE_REFUND_REQUEST_SEQ, "issue_refund", ISSUE_REFUND_ARGS)
    state, refund_count, recovered = asyncio.run(_verify(run_id, key))

    assert state.status == "completed"
    assert refund_count == 1  # the tool really was called twice; only one refund exists
    assert recovered is True  # the resumed process, not the killed one, finished this step
