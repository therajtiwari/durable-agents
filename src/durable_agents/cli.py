import argparse
import asyncio
import os
import sys
from uuid import UUID, uuid4

from durable_agents.events import LLMCallCompleted
from durable_agents.llm.openai_compatible import OpenAICompatibleClient
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


async def _demo(run_id: UUID, dsn: str) -> None:
    """The zero-setup crash-resume proof: one fixed scripted conversation
    against fake refund tools — no API key, no network call, nothing to
    configure. Starting is just resuming from an empty log, same as
    orchestrator.run() itself, so the same function handles both a fresh
    run_id and one that already has events (e.g. was killed mid-flight)
    — reconstructing ScriptedLLM's position from how many
    LLMCallCompleted events already exist.

    This is a fixed demo, not a way to run an arbitrary goal — see
    `resume` for that.
    """

    store = await PostgresEventStore.connect(dsn)
    events = await store.read(run_id)

    if not events:
        await store.append(run_id, 0, canonical_run_started(requested_by="cli-demo"))
        events = await store.read(run_id)

    already_completed = sum(1 for e in events if isinstance(e, LLMCallCompleted))
    llm = ScriptedLLM(canonical_script()[already_completed:])

    backend = await PostgresRefundBackend.connect(dsn)
    tools = {t.name: t for t in build_refund_tools(backend)}

    orchestrator = Orchestrator(store=store, llm=llm, tools=tools)
    final_state = await orchestrator.run(run_id)

    print(f"Run {run_id}: {final_state.status}")
    print(f"(see the full trace with: durable-agents replay {run_id})")


async def _resume(run_id: UUID, dsn: str) -> None:
    """Resume ANY run — including one created over the API with an
    arbitrary goal — using a real LLM client.

    Configured entirely through environment variables (LLM_API_KEY /
    LLM_BASE_URL / LLM_MODEL), the same convention tests/live and the
    examples/live_*.py scripts already use, rather than a hardcoded
    provider — see DECISIONS.md's "Provider client" section for why.

    Runs with NO tools: this command has no way to know what functions a
    specific deployment wants wired up for a given run, so it only
    handles pure conversational/reasoning goals. A run that needs real
    tools needs a real script wired against Runtime/Orchestrator
    directly — see README.md's "Bring your own model" section.
    """

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        print("Set LLM_API_KEY to resume a run with a real LLM (a free Groq key works).")
        print("This command runs with no tools — only goals that need pure reasoning,")
        print("no tool calls, will complete. For anything else, write a script against")
        print("Runtime/Orchestrator directly (see README.md).")
        sys.exit(1)

    base_url = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    model = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")

    store = await PostgresEventStore.connect(dsn)
    llm = OpenAICompatibleClient(base_url=base_url, model=model, api_key=api_key)
    try:
        orchestrator = Orchestrator(store=store, llm=llm, tools={})
        final_state = await orchestrator.run(run_id)
    finally:
        await llm.aclose()

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

    demo_parser = subparsers.add_parser(
        "demo",
        help="Run the zero-setup crash-resume demo (fixed scripted refund scenario)",
    )
    demo_parser.add_argument(
        "run_id",
        type=UUID,
        nargs="?",
        default=None,
        help="Resume a demo run that was killed mid-flight; omit to start a new one",
    )
    demo_parser.add_argument(
        "--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN)
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume ANY run with a real LLM (needs LLM_API_KEY; no tools wired up)",
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
    elif args.command == "demo":
        run_id = args.run_id or uuid4()
        if args.run_id is None:
            print(f"Starting demo run {run_id}")
        asyncio.run(_demo(run_id, args.dsn))
    elif args.command == "resume":
        asyncio.run(_resume(args.run_id, args.dsn))


if __name__ == "__main__":
    main()
