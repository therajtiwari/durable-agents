import argparse
import asyncio
import os
from typing import assert_never
from uuid import UUID, uuid4

from durable_agents.events import (
    ApprovalDenied,
    ApprovalGranted,
    ApprovalRequested,
    Event,
    GuardrailTriggered,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallRequested,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
)
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.state import rebuild_state
from durable_agents.storage.postgres import PostgresEventStore
from durable_agents.tools.refund_backend_postgres import PostgresRefundBackend
from durable_agents.tools.refund_demo_scenario import canonical_run_started, canonical_script
from durable_agents.tools.refund_tools import build_refund_tools

DEFAULT_DSN = "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"


def _event_detail(event: Event) -> str:
    """One human-readable line per event type — this is the whole point
    of event sourcing made visible: no logging code was written to get
    this, it's read straight out of the log.
    """

    match event:
        case RunStarted():
            return (
                f"goal={event.goal!r} model={event.model} "
                f"max_steps={event.max_steps} max_cost=${event.max_cost_usd}"
            )
        case LLMCallRequested():
            return (
                f"step={event.step} messages={event.message_count} "
                f"est_tokens={event.estimated_tokens}"
            )
        case LLMCallCompleted():
            if event.tool_calls:
                calls = ", ".join(f"{tc.name}({tc.arguments})" for tc in event.tool_calls)
                action = f"-> {calls}"
            else:
                action = f"-> {event.content!r}"
            return (
                f"step={event.step} {action} [{event.input_tokens} in / "
                f"{event.output_tokens} out tok, ${event.cost_usd}, {event.latency_ms}ms]"
            )
        case LLMCallFailed():
            return f"step={event.step} attempt={event.attempt} error={event.error!r}"
        case ToolCallRequested():
            return f"step={event.step} {event.tool}({event.arguments})"
        case ToolCallCompleted():
            recovered = " [recovered]" if event.recovered else ""
            return (
                f"step={event.step} {event.tool} -> {event.result} "
                f"[{event.duration_ms}ms]{recovered}"
            )
        case ToolCallFailed():
            return f"step={event.step} {event.tool} attempt={event.attempt} error={event.error!r}"
        case GuardrailTriggered():
            return f"{event.layer} rule={event.rule} action={event.action}"
        case ApprovalRequested():
            return f"step={event.step} {event.tool}({event.arguments}) reason={event.reason!r}"
        case ApprovalGranted():
            return f"approver={event.approver}"
        case ApprovalDenied():
            return f"approver={event.approver} reason={event.reason!r}"
        case RunCompleted():
            return (
                f"final_answer={event.final_answer!r} steps={event.total_steps} "
                f"tokens={event.total_tokens} cost=${event.total_cost_usd}"
            )
        case RunFailed():
            return f"reason={event.reason}"
        case _:
            assert_never(event)


async def _replay(run_id: UUID, dsn: str) -> None:
    store = await PostgresEventStore.connect(dsn)
    events = await store.read(run_id)

    if not events:
        print(f"No events found for run {run_id}")
        return

    print(f"Run {run_id}")
    print("=" * 78)
    for event in events:
        timestamp = event.created_at.strftime("%H:%M:%S.%f")[:-3]
        type_name = type(event).__name__
        print(f"seq={event.seq:3d}  {timestamp}  {type_name:<18} {_event_detail(event)}")

    state = rebuild_state(events)
    duration = (events[-1].created_at - events[0].created_at).total_seconds()

    print("=" * 78)
    print(
        f"status: {state.status}  |  steps: {state.step}  |  "
        f"tokens: {state.total_tokens}  |  cost: ${state.total_cost_usd}  |  "
        f"duration: {duration:.2f}s"
    )
    if state.final_answer:
        print(f"final answer: {state.final_answer}")
    if state.failure_reason:
        print(f"failure reason: {state.failure_reason}")


async def _start_or_resume(run_id: UUID, dsn: str) -> None:
    """Shared by both start and resume — deliberately the same code path,
    same as orchestrator.run() itself: starting is just resuming from an
    empty log. Runs the one fixed canonical demo scenario (no real LLM
    client exists yet — only ScriptedLLM and the fake refund backend are
    built), reconstructing ScriptedLLM's position from how many
    LLMCallCompleted events already exist, so calling this twice on the
    same run_id (e.g. after a crash) picks up correctly.
    """

    store = await PostgresEventStore.connect(dsn)
    events = await store.read(run_id)

    if not events:
        await store.append(run_id, 0, canonical_run_started(requested_by="cli"))
        events = await store.read(run_id)

    already_completed = sum(1 for e in events if isinstance(e, LLMCallCompleted))
    llm = ScriptedLLM(canonical_script()[already_completed:])

    backend = await PostgresRefundBackend.connect(dsn)
    tools = {t.name: t for t in build_refund_tools(backend)}

    orchestrator = Orchestrator(store=store, llm=llm, tools=tools)
    final_state = await orchestrator.run(run_id)

    print(f"Run {run_id}: {final_state.status}")
    print(f"(see the full trace with: durable-agents replay {run_id})")


def main() -> None:
    parser = argparse.ArgumentParser(prog="durable-agents")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay_parser = subparsers.add_parser(
        "replay", help="Print the full event trace for a run"
    )
    replay_parser.add_argument("run_id", type=UUID)
    replay_parser.add_argument(
        "--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN)
    )

    start_parser = subparsers.add_parser(
        "start", help="Start a new run of the demo refund scenario"
    )
    start_parser.add_argument(
        "--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN)
    )

    resume_parser = subparsers.add_parser(
        "resume", help="Resume an existing run from wherever its event log left off"
    )
    resume_parser.add_argument("run_id", type=UUID)
    resume_parser.add_argument(
        "--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN)
    )

    args = parser.parse_args()

    if args.command == "replay":
        asyncio.run(_replay(args.run_id, args.dsn))
    elif args.command == "start":
        run_id = uuid4()
        print(f"Starting run {run_id}")
        asyncio.run(_start_or_resume(run_id, args.dsn))
    elif args.command == "resume":
        asyncio.run(_start_or_resume(args.run_id, args.dsn))


if __name__ == "__main__":
    main()
