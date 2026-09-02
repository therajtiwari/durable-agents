"""An event-sourced runtime for durable, crash-resumable LLM agents.

Everything an agent does — every model call, every tool call, every
guardrail decision, every human approval — is appended to a log before
and after it happens. State is a pure fold over that log, so a process
that dies mid-run can be resumed by a different process that reads the
log and finishes the job, without repeating side effects that already
happened.

    from durable_agents import Runtime, InMemoryEventStore, tool

    @tool(side_effect=True)
    async def issue_refund(order_id: str, amount: int, idempotency_key: str) -> dict:
        return await payments.refund(order_id, amount, key=idempotency_key)

    runtime = Runtime(store=InMemoryEventStore(), llm=my_client, tools=[issue_refund])
    state = await runtime.start(goal="Refund order A-8891, item arrived damaged.")

Swap InMemoryEventStore for PostgresEventStore when you want the run to
outlive the process. See README.md for the full walkthrough.
"""

from durable_agents.events import (
    ApprovalDenied,
    ApprovalGranted,
    ApprovalRequested,
    Event,
    GuardrailAction,
    GuardrailLayer,
    GuardrailTriggered,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallRequested,
    RunCompleted,
    RunFailed,
    RunFailureReason,
    RunStarted,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallInvocation,
    ToolCallRequested,
    hash_system_prompt,
)
from durable_agents.guardrails.decisions import PROFILES, GuardrailProfile, decide, get_profile
from durable_agents.guardrails.types import GuardMatch, ScanResult
from durable_agents.llm.protocol import LLMClient, LLMResponse
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.runtime import Run, Runtime
from durable_agents.state import (
    GuardrailHit,
    InFlightOp,
    Message,
    PendingApproval,
    RunState,
    RunStatus,
    rebuild_state,
)
from durable_agents.storage.memory import InMemoryEventStore
from durable_agents.storage.postgres import PostgresEventStore
from durable_agents.storage.protocol import ConcurrencyConflict, EventStore
from durable_agents.storage.schema import create_schema, schema_sql
from durable_agents.tools.registry import Tool, idempotency_key, tool

__version__ = "0.1.0"

__all__ = [
    # The five names most users need
    "Runtime",
    "Run",
    "tool",
    "InMemoryEventStore",
    "PostgresEventStore",
    "create_schema",
    # Implement one of these to plug in your model provider
    "LLMClient",
    "LLMResponse",
    "ScriptedLLM",
    # Storage
    "EventStore",
    "ConcurrencyConflict",
    "schema_sql",
    # The loop itself, for anyone who wants it without the Runtime facade
    "Orchestrator",
    # Derived state
    "RunState",
    "RunStatus",
    "Message",
    "InFlightOp",
    "PendingApproval",
    "GuardrailHit",
    "rebuild_state",
    # Tools
    "Tool",
    "idempotency_key",
    # Guardrails
    "GuardrailProfile",
    "PROFILES",
    "decide",
    "get_profile",
    "GuardMatch",
    "ScanResult",
    # Events — the actual source of truth, worth having close to hand
    "Event",
    "RunStarted",
    "RunCompleted",
    "RunFailed",
    "RunFailureReason",
    "LLMCallRequested",
    "LLMCallCompleted",
    "LLMCallFailed",
    "ToolCallInvocation",
    "ToolCallRequested",
    "ToolCallCompleted",
    "ToolCallFailed",
    "ApprovalRequested",
    "ApprovalGranted",
    "ApprovalDenied",
    "GuardrailTriggered",
    "GuardrailLayer",
    "GuardrailAction",
    "hash_system_prompt",
    "__version__",
]
