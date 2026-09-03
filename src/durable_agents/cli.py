import argparse
import asyncio
import os
import sys
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from durable_agents.orchestrator import Orchestrator
from durable_agents.replay_view import Style, render, should_use_colour
from durable_agents.state import rebuild_state
from durable_agents.storage.postgres import PostgresEventStore
from durable_agents.storage.schema import create_schema

# OpenAICompatibleClient is imported inside the one subcommand that uses
# it, not here. It pulls in httpx, which lives in the optional "openai"
# extra — importing it at module scope meant `pip install
# durable-agents` followed by `durable-agents --help` died with
# ModuleNotFoundError, i.e. the entry point every doc points at was
# broken on a plain install.
#
# Every subcommand here is generic. The fixed refund demo used to live
# alongside them as `durable-agents demo`; it is now
# examples/crash_resume_demo.py, since a library's console script has no
# business shipping a scripted demo of someone else's domain — and it
# could not have kept working anyway once the refund modules stopped
# being part of the package.

DEFAULT_DSN = "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"


def redact_dsn(dsn: str) -> str:
    """A connection string with the password starred out, safe to print.

    Which database was touched is genuinely useful to see; the password
    sitting next to it is not, and stdout is exactly where credentials
    escape — terminal scrollback, CI logs, screen shares. An
    unparseable string is reported as-is minus everything before the
    "@", since guessing at its structure risks leaking the very thing
    this exists to hide.
    """

    try:
        parts = urlsplit(dsn)
    except ValueError:
        return dsn.rsplit("@", 1)[-1]

    # urlsplit does not raise on a malformed connection string — given
    # "postgres//user:pw@host/db" (one slash missing) it happily reports
    # no netloc and no password, and returning the input unchanged would
    # then print the password in full. A visible "@" with nothing parsed
    # around it means the structure is not what it looks like, so drop
    # everything before it rather than trusting the parse.
    if not parts.netloc:
        return dsn.rsplit("@", 1)[-1] if "@" in dsn else dsn

    if parts.password is None:
        return dsn

    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = f"{parts.username}:***@" if parts.username else "***@"
    return urlunsplit((parts.scheme, f"{userinfo}{host}", parts.path, parts.query, parts.fragment))



async def _replay(run_id: UUID, dsn: str, *, colour: bool | None, show_thinking: bool) -> None:
    store = await PostgresEventStore.connect(dsn)
    events = await store.read(run_id)

    if not events:
        print(f"No events found for run {run_id}")
        return

    style = Style.enabled() if should_use_colour(colour) else Style()
    print(render(run_id, events, rebuild_state(events), style, show_thinking))


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

    try:
        from durable_agents.llm.openai_compatible import OpenAICompatibleClient
    except ImportError as exc:
        print(f"This command needs the 'openai' extra: pip install 'durable-agents[openai]'  ({exc})")
        sys.exit(1)

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
        print(f"Schema ready on {redact_dsn(args.dsn)}")
    elif args.command == "replay":
        asyncio.run(
            _replay(
                args.run_id,
                args.dsn,
                colour=False if args.no_color else None,
                show_thinking=args.thinking,
            )
        )
    elif args.command == "resume":
        asyncio.run(_resume(args.run_id, args.dsn))


if __name__ == "__main__":
    main()
