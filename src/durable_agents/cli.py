import argparse
import asyncio
import os
import sys
from uuid import UUID, uuid4

from durable_agents.events import LLMCallCompleted
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.replay_view import Style, render, should_use_colour
from durable_agents.state import rebuild_state
from durable_agents.storage.postgres import PostgresEventStore
from durable_agents.storage.schema import create_schema
from durable_agents.tools.refund_backend_postgres import PostgresRefundBackend
from durable_agents.tools.refund_demo_scenario import canonical_run_started, canonical_script
from durable_agents.tools.refund_tools import build_refund_tools

DEFAULT_DSN = "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"



async def _replay(run_id: UUID, dsn: str, *, colour: bool | None, show_thinking: bool) -> None:
    store = await PostgresEventStore.connect(dsn)
    events = await store.read(run_id)

    if not events:
        print(f"No events found for run {run_id}")
        return

    style = Style.enabled() if should_use_colour(colour) else Style()
    print(render(run_id, events, rebuild_state(events), style, show_thinking))


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
    # Windows consoles still default to a legacy codepage (cp1252),
    # which raises UnicodeEncodeError on any non-ASCII character in a
    # goal, tool result, or final answer — i.e. on most of the world's
    # text. Printing a run's own recorded content must not depend on
    # the operator's locale.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="durable-agents")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay_parser = subparsers.add_parser(
        "replay", help="Print the full event trace for a run"
    )
    replay_parser.add_argument("run_id", type=UUID)
    replay_parser.add_argument(
        "--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN)
    )
    replay_parser.add_argument(
        "--thinking",
        action="store_true",
        help="Include LLMCallRequested events (hidden by default as noise)",
    )
    replay_parser.add_argument(
        "--no-color", action="store_true", help="Disable coloured output"
    )

    init_db_parser = subparsers.add_parser(
        "init-db", help="Create the events table (idempotent, safe to re-run)"
    )
    init_db_parser.add_argument(
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

    if args.command == "init-db":
        asyncio.run(create_schema(args.dsn))
        print(f"Schema ready on {args.dsn}")
    elif args.command == "replay":
        asyncio.run(
            _replay(
                args.run_id,
                args.dsn,
                colour=False if args.no_color else None,
                show_thinking=args.thinking,
            )
        )
    elif args.command == "start":
        run_id = uuid4()
        print(f"Starting run {run_id}")
        asyncio.run(_start_or_resume(run_id, args.dsn))
    elif args.command == "resume":
        asyncio.run(_start_or_resume(args.run_id, args.dsn))


if __name__ == "__main__":
    main()
