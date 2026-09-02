"""Rendering for `durable-agents replay`.

Kept out of cli.py because formatting a trace is a genuinely separate
concern from parsing arguments and talking to Postgres, and because the
web demo will want to render the same events without going through
argparse.

Output is deliberately plain ASCII: this gets read over SSH, piped into
files, and pasted into tickets, none of which reliably survive box
drawing characters or a Windows console's legacy codepage.
"""

import json
import os
import sys
import textwrap
from dataclasses import dataclass
from typing import Any, assert_never

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
from durable_agents.state import RunState

WIDTH = 78
GUTTER = 15


@dataclass(frozen=True)
class Style:
    """ANSI codes, or empty strings when colour is off. Every caller
    concatenates unconditionally, so disabling colour never needs a
    branch at the call site.

    No blue: ANSI blue is close to unreadable on a dark background, and
    any hardcoded hue is wrong on somebody's theme. Emphasis that isn't
    conveying success/failure uses bold in the terminal's own
    foreground colour instead.
    """

    dim: str = ""
    bold: str = ""
    red: str = ""
    yellow: str = ""
    green: str = ""
    magenta: str = ""
    reset: str = ""

    @classmethod
    def enabled(cls) -> "Style":
        return cls(
            dim="\033[2m",
            bold="\033[1m",
            red="\033[31m",
            yellow="\033[33m",
            green="\033[32m",
            magenta="\033[35m",
            reset="\033[0m",
        )


def should_use_colour(explicit: bool | None = None) -> bool:
    """Colour only when it can't hurt: a real terminal, not redirected
    to a file, and not suppressed by the NO_COLOR convention
    (no-color.org, respected by a growing number of CLIs).
    """

    if explicit is not None:
        return explicit
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        # Windows needs virtual-terminal processing switched on before
        # ANSI codes render rather than printing as literal escapes.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            # -11 = STD_OUTPUT_HANDLE, 7 = ENABLE_PROCESSED_OUTPUT |
            # ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return bool(kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7))
        except Exception:
            return False
    return True


def _fmt_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    # ensure_ascii=False so text stays readable rather than turning into
    # \uXXXX escapes in a trace meant for humans.
    return json.dumps(value, default=str, ensure_ascii=False)


def _fmt_args(arguments: dict[str, Any]) -> str:
    if not arguments:
        return "()"
    return "(" + ", ".join(f"{k}={_fmt_value(v)}" for k, v in arguments.items()) + ")"


@dataclass(frozen=True)
class Row:
    """One rendered event: a two-character marker, a headline, and
    optional detail lines indented underneath it. Markers are all the
    same width so the headlines line up in a column.
    """

    marker: str
    colour: str
    headline: str
    details: list[str]


def render_event(event: Event, s: Style) -> Row:
    match event:
        case RunStarted():
            details = [
                f"model {event.model}  |  max {event.max_steps} steps  |  cap ${event.max_cost_usd}"
            ]
            if event.system_prompt:
                details.append(f"system: {event.system_prompt}")
            return Row("* ", s.bold, f"run started: {event.goal}", details)

        case LLMCallRequested():
            return Row(". ", s.dim, f"thinking ({event.message_count} messages)", [])

        case LLMCallCompleted():
            cost = (
                f"{event.input_tokens}+{event.output_tokens} tok"
                f"  ${event.cost_usd}  {event.latency_ms}ms"
            )
            if event.tool_calls:
                calls = ", ".join(f"{c.name}{_fmt_args(c.arguments)}" for c in event.tool_calls)
                return Row("> ", s.bold, f"model decided: {calls}", [s.dim + cost + s.reset])
            return Row(
                "> ", s.bold, f"model said: {event.content or ''}", [s.dim + cost + s.reset]
            )

        case LLMCallFailed():
            return Row("!!", s.red, f"model call failed (attempt {event.attempt})", [event.error])

        case ToolCallRequested():
            return Row("->", s.reset, f"calling {event.tool}{_fmt_args(event.arguments)}", [])

        case ToolCallCompleted():
            flags = []
            if event.recovered:
                flags.append("recovered by another process")
            if event.provider_dedup_hit:
                flags.append("deduplicated, side effect already done")
            details = [_fmt_value(event.result)]
            if flags:
                details.append(s.magenta + ", ".join(flags) + s.reset)
            return Row(
                "ok",
                s.green,
                f"{event.tool} returned  {s.dim}({event.duration_ms}ms){s.reset}",
                details,
            )

        case ToolCallFailed():
            suffix = "" if event.final_attempt else ", will retry with the same idempotency key"
            return Row(
                "!!",
                s.red,
                f"{event.tool} failed (attempt {event.attempt}){suffix}",
                [event.error],
            )

        case GuardrailTriggered():
            return Row(
                "!!",
                s.yellow,
                f"guardrail {event.action}: {event.rule} [{event.layer}]",
                [_fmt_value(event.detail)],
            )

        case ApprovalRequested():
            return Row(
                "##",
                s.yellow + s.bold,
                f"PAUSED, needs human approval: {event.tool}{_fmt_args(event.arguments)}",
                [event.reason],
            )

        case ApprovalGranted():
            return Row("ok", s.green + s.bold, f"approved by {event.approver}", [])

        case ApprovalDenied():
            return Row("no", s.red + s.bold, f"denied by {event.approver}", [event.reason])

        case RunCompleted():
            return Row(
                "* ",
                s.green + s.bold,
                f"run completed: {event.final_answer}",
                [
                    f"{event.total_steps} steps  |  {event.total_tokens} tokens"
                    f"  |  ${event.total_cost_usd}"
                ],
            )

        case RunFailed():
            return Row("* ", s.red + s.bold, f"run failed: {event.reason}", [event.detail or ""])

        case _:
            assert_never(event)


def _step_of(event: Event) -> int | None:
    return getattr(event, "step", None)


def _wrap(text: str, indent: str) -> list[str]:
    """Wrap to the terminal width with a hanging indent, so a long tool
    result stays fully readable instead of becoming one runaway line.
    Nothing is elided: a trace that hides part of what happened is not
    an audit trail.
    """

    lines: list[str] = []
    for paragraph in text.split("\n"):
        wrapped = textwrap.wrap(paragraph, width=WIDTH - len(indent)) or [""]
        lines.extend(wrapped)
    return [indent + line for line in lines]


def render(
    run_id: Any, events: list[Event], state: RunState, s: Style, show_thinking: bool
) -> str:
    if not events:
        return f"No events found for run {run_id}"

    out: list[str] = []
    status_colour = {
        "completed": s.green,
        "failed": s.red,
        "awaiting_approval": s.yellow,
    }.get(state.status, s.reset)
    duration = (events[-1].created_at - events[0].created_at).total_seconds()

    out.append(f"{s.bold}run {run_id}{s.reset}")
    out.append(
        f"{status_colour}{s.bold}{state.status.upper()}{s.reset}"
        f"{s.dim}  |  {state.step} steps  |  {state.total_tokens} tokens"
        f"  |  ${state.total_cost_usd}  |  {duration:.1f}s  |  {len(events)} events{s.reset}"
    )
    out.append(s.dim + "-" * WIDTH + s.reset)

    detail_indent = " " * GUTTER + "| "
    current_step: int | None = None
    for event in events:
        if isinstance(event, LLMCallRequested) and not show_thinking:
            continue

        step = _step_of(event)
        if step is not None and step != current_step:
            current_step = step
            label = f"-- step {step} "
            out.append("")
            out.append(f"{s.dim}{label}{'-' * (WIDTH - len(label))}{s.reset}")

        row = render_event(event, s)
        stamp = event.created_at.strftime("%H:%M:%S")
        prefix = f"{s.dim}{event.seq:>3} {stamp}{s.reset}  {row.colour}{row.marker} "
        # Wrapped with a hanging indent aligned under the headline, so
        # the seq and timestamp stay on the first line (keeping a line
        # greppable by seq) while a long tool-call argument list still
        # stays on screen.
        headline_lines = textwrap.wrap(row.headline, width=WIDTH - GUTTER - 3) or [""]
        out.append(prefix + headline_lines[0] + s.reset)
        for continuation in headline_lines[1:]:
            out.append(" " * (GUTTER + 3) + row.colour + continuation + s.reset)
        for detail in row.details:
            if detail:
                out.extend(_wrap(detail, detail_indent))

    out.append("")
    out.append(s.dim + "-" * WIDTH + s.reset)
    if state.final_answer:
        out.extend(_wrap(f"answer: {state.final_answer}", ""))
    if state.failure_reason:
        out.append(f"{s.red}{s.bold}failed:{s.reset} {state.failure_reason}")
    if state.pending_approval is not None:
        pending = state.pending_approval
        out.extend(_wrap(f"waiting on: {pending.tool}{_fmt_args(pending.arguments)}", ""))
    if state.guardrail_hits:
        blocked = sum(1 for h in state.guardrail_hits if h.action == "BLOCK")
        out.append(
            f"{s.yellow}guardrails:{s.reset} {len(state.guardrail_hits)} triggered"
            + (f", {blocked} blocking" if blocked else "")
        )

    return "\n".join(out)
