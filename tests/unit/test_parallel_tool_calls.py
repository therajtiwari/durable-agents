"""A model may ask for several tools in one response.

Before this was supported the orchestrator executed `tool_calls[0]` and
silently discarded the rest, then reported the run `completed` — the
caller got a confident answer to part of their question. The wire format
was broken too: providers reject an assistant turn whose tool_calls
aren't each answered, so a real run died on a 400 that pointed at message
formatting rather than at the missing work.

These pin down both halves, plus the cases the change put at risk:
approval, mid-batch resume, and logs written before any of this existed.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from durable_agents.events import (
    ApprovalDenied,
    ApprovalGranted,
    Event,
    LLMCallCompleted,
    LLMCallRequested,
    RunStarted,
    ToolCallCompleted,
    ToolCallInvocation,
    ToolCallRequested,
)
from durable_agents.llm.openai_compatible import OpenAICompatibleClient
from durable_agents.llm.protocol import LLMResponse
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.state import rebuild_state
from durable_agents.storage.memory import InMemoryEventStore
from durable_agents.tools.registry import Tool, tool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_started() -> RunStarted:
    return RunStarted(
        seq=0,
        created_at=_now(),
        goal="Weather in Paris, Tokyo and Lima?",
        model="scripted",
        max_steps=15,
        max_cost_usd=Decimal("2.00"),
        requested_by="test",
        guardrail_profile="standard",
    )


def _llm_response(**overrides: object) -> LLMResponse:
    defaults: dict[str, object] = {
        "content": None,
        "tool_calls": [],
        "stop_reason": "end_turn",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": Decimal("0.001"),
        "latency_ms": 500,
        "provider_request_id": "req",
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)  # type: ignore[arg-type]


def _tools() -> tuple[dict[str, Tool], list[str]]:
    """Tools plus the ledger of what physically ran, so a test can tell
    "the log says it happened" apart from "the function was called".
    """

    executed: list[str] = []

    @tool()
    async def get_weather(city: str) -> dict[str, Any]:
        executed.append(f"weather:{city}")
        return {"city": city, "temp_c": 20}

    @tool(requires_approval=True, side_effect=True)
    async def wipe_disk(host: str) -> dict[str, Any]:
        executed.append(f"wipe:{host}")
        return {"host": host, "wiped": True}

    return {t.name: t for t in (get_weather, wipe_disk)}, executed


def _three_city_call() -> LLMResponse:
    return _llm_response(
        content="Checking all three.",
        tool_calls=[
            ToolCallInvocation(id="c1", name="get_weather", arguments={"city": "Paris"}),
            ToolCallInvocation(id="c2", name="get_weather", arguments={"city": "Tokyo"}),
            ToolCallInvocation(id="c3", name="get_weather", arguments={"city": "Lima"}),
        ],
        stop_reason="tool_use",
    )


@pytest.mark.asyncio
async def test_every_call_in_a_batch_executes() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())
    tools, executed = _tools()

    llm = ScriptedLLM([_three_city_call(), _llm_response(content="All three are 20C.")])
    state = await Orchestrator(store=store, llm=llm, tools=tools).run(run_id)

    assert executed == ["weather:Paris", "weather:Tokyo", "weather:Lima"]
    assert state.status == "completed"

    # One Requested/Completed pair per call, and the model is called again
    # only after the whole batch is answered — not once per tool.
    events = await store.read(run_id)
    assert [type(e).__name__ for e in events].count("ToolCallRequested") == 3
    assert [type(e).__name__ for e in events].count("ToolCallCompleted") == 3
    assert llm.call_count == 2

    # Each pair carries the model's own id, so results can be matched back.
    requested = [e for e in events if isinstance(e, ToolCallRequested)]
    assert [e.tool_call_id for e in requested] == ["c1", "c2", "c3"]
    # Distinct idempotency keys: three separate side effects, not one retried.
    assert len({e.idempotency_key for e in requested}) == 3


@pytest.mark.asyncio
async def test_wire_format_answers_every_tool_call_id() -> None:
    """The half that made real providers 400: N tool_calls must come back
    with N tool messages carrying matching ids.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())
    tools, _ = _tools()

    llm = ScriptedLLM([_three_city_call(), _llm_response(content="Done.")])
    await Orchestrator(store=store, llm=llm, tools=tools).run(run_id)

    state = rebuild_state(await store.read(run_id))
    client = OpenAICompatibleClient(base_url="http://example.invalid/v1", model="m")
    wire = client._to_openai_messages(state.messages, "")

    requested_ids = [
        call["id"] for m in wire if m["role"] == "assistant" for call in m.get("tool_calls", [])
    ]
    answered_ids = [m["tool_call_id"] for m in wire if m["role"] == "tool"]
    assert requested_ids == ["c1", "c2", "c3"]
    assert sorted(answered_ids) == ["c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_resume_mid_batch_runs_only_what_is_left() -> None:
    """A process that died after two of three calls must finish the third
    and re-run neither of the first two.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    tools, executed = _tools()

    # The log a process leaves behind having answered c1 and c2 only.
    log: list[Event] = [
        _run_started(),
        LLMCallRequested(seq=1, created_at=_now(), step=1, message_count=1, estimated_tokens=10),
        LLMCallCompleted(
            seq=2,
            created_at=_now(),
            step=1,
            content="Checking all three.",
            tool_calls=_three_city_call().tool_calls,
            stop_reason="tool_use",
            input_tokens=100,
            output_tokens=20,
            cost_usd=Decimal("0.001"),
            latency_ms=500,
            provider_request_id="req",
        ),
    ]
    seq = 3
    for call_id, city in (("c1", "Paris"), ("c2", "Tokyo")):
        log.append(
            ToolCallRequested(
                seq=seq,
                created_at=_now(),
                step=1,
                tool="get_weather",
                arguments={"city": city},
                idempotency_key=f"key-{call_id}",
                requires_approval=False,
                tool_call_id=call_id,
            )
        )
        log.append(
            ToolCallCompleted(
                seq=seq + 1,
                created_at=_now(),
                step=1,
                tool="get_weather",
                idempotency_key=f"key-{call_id}",
                result={"city": city, "temp_c": 20},
                duration_ms=5,
                recovered=False,
                provider_dedup_hit=False,
                tool_call_id=call_id,
            )
        )
        seq += 2
    for i, event in enumerate(log):
        await store.append(run_id, i, event)

    llm = ScriptedLLM([_llm_response(content="All three are 20C.")])
    state = await Orchestrator(store=store, llm=llm, tools=tools).run(run_id)

    assert executed == ["weather:Lima"]
    assert state.status == "completed"


@pytest.mark.asyncio
async def test_approving_one_call_does_not_release_another() -> None:
    """Approval is per call. A step carrying two calls that both need a
    human must ask twice — approving the first must not wave the second
    through.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())
    tools, executed = _tools()

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Wiping both.",
                tool_calls=[
                    ToolCallInvocation(id="w1", name="wipe_disk", arguments={"host": "alpha"}),
                    ToolCallInvocation(id="w2", name="wipe_disk", arguments={"host": "beta"}),
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="Both wiped."),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=llm, tools=tools)

    state = await orchestrator.run(run_id)
    assert state.status == "awaiting_approval"
    assert state.pending_approval is not None
    assert state.pending_approval.tool_call_id == "w1"
    assert executed == []

    events = await store.read(run_id)
    await store.append(
        run_id, len(events), ApprovalGranted(seq=len(events), created_at=_now(), approver="dana")
    )

    state = await orchestrator.run(run_id)

    # The granted call ran; the second parked for its own decision rather
    # than riding in on the first one's approval.
    assert executed == ["wipe:alpha"]
    assert state.status == "awaiting_approval"
    assert state.pending_approval is not None
    assert state.pending_approval.tool_call_id == "w2"


@pytest.mark.asyncio
async def test_legacy_log_without_ids_does_not_re_execute() -> None:
    """Events written before tool_call_id existed carry no ids at all.
    Such a log only ever had one call outstanding, so an id-less result
    answers the call it follows — and must not look unanswered, which
    would re-run a side effect that already happened.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    tools, executed = _tools()

    log: list[Event] = [
        _run_started(),
        LLMCallRequested(seq=1, created_at=_now(), step=1, message_count=1, estimated_tokens=10),
        LLMCallCompleted(
            seq=2,
            created_at=_now(),
            step=1,
            content="Checking Paris.",
            tool_calls=[
                ToolCallInvocation(id="legacy-1", name="get_weather", arguments={"city": "Paris"})
            ],
            stop_reason="tool_use",
            input_tokens=100,
            output_tokens=20,
            cost_usd=Decimal("0.001"),
            latency_ms=500,
            provider_request_id="req",
        ),
        # Written before the field existed: tool_call_id defaults to "".
        ToolCallRequested(
            seq=3,
            created_at=_now(),
            step=1,
            tool="get_weather",
            arguments={"city": "Paris"},
            idempotency_key="legacy-key",
            requires_approval=False,
        ),
        ToolCallCompleted(
            seq=4,
            created_at=_now(),
            step=1,
            tool="get_weather",
            idempotency_key="legacy-key",
            result={"city": "Paris", "temp_c": 20},
            duration_ms=5,
            recovered=False,
            provider_dedup_hit=False,
        ),
    ]
    for i, event in enumerate(log):
        await store.append(run_id, i, event)

    llm = ScriptedLLM([_llm_response(content="Paris is 20C.")])
    state = await Orchestrator(store=store, llm=llm, tools=tools).run(run_id)

    assert executed == []
    assert state.status == "completed"

    # And that log still renders a valid conversation for the provider.
    client = OpenAICompatibleClient(base_url="http://example.invalid/v1", model="m")
    wire = client._to_openai_messages(rebuild_state(await store.read(run_id)).messages, "")
    assert [m["tool_call_id"] for m in wire if m["role"] == "tool"] == ["legacy-1"]


# --- when one call in a batch goes wrong -------------------------------
#
# Each of these resolves ONE call by some route other than a clean
# ToolCallCompleted. Every such route has to mark that call answered, or
# decide_next_action proposes it forever and the run spins or parks for
# good. They are separate code paths, so they need separate tests.


@pytest.mark.asyncio
async def test_denied_call_does_not_stall_the_rest_of_the_batch() -> None:
    """A human refusing one call must let the batch carry on. If the
    denial does not mark that call answered, it gets proposed again,
    re-triggers requires_approval, and parks forever.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())
    tools, executed = _tools()

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Wiping beta, checking Paris.",
                tool_calls=[
                    ToolCallInvocation(id="w1", name="wipe_disk", arguments={"host": "beta"}),
                    ToolCallInvocation(id="c1", name="get_weather", arguments={"city": "Paris"}),
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="Skipped the wipe; Paris is 20C."),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=llm, tools=tools)

    state = await orchestrator.run(run_id)
    assert state.status == "awaiting_approval"
    assert state.pending_approval is not None
    assert state.pending_approval.tool_call_id == "w1"

    events = await store.read(run_id)
    await store.append(
        run_id,
        len(events),
        ApprovalDenied(
            seq=len(events), created_at=_now(), approver="dana", reason="beta is still in use"
        ),
    )

    state = await orchestrator.run(run_id)

    assert executed == ["weather:Paris"], "denied call must not run, its sibling must"
    assert state.status == "completed"


@pytest.mark.asyncio
async def test_permanently_failing_call_does_not_stall_the_batch() -> None:
    """A call whose retry budget is spent is answered by surfacing the
    error to the model, and the rest of the batch continues.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    attempts: list[str] = []

    @tool()
    async def always_breaks(thing: str) -> dict[str, Any]:
        attempts.append(thing)
        raise RuntimeError("upstream is down")

    @tool()
    async def get_weather(city: str) -> dict[str, Any]:
        attempts.append(f"weather:{city}")
        return {"city": city, "temp_c": 20}

    tools = {t.name: t for t in (always_breaks, get_weather)}

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Both, please.",
                tool_calls=[
                    ToolCallInvocation(id="b1", name="always_breaks", arguments={"thing": "x"}),
                    ToolCallInvocation(id="c1", name="get_weather", arguments={"city": "Lima"}),
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="One failed, Lima is 20C."),
        ]
    )
    state = await Orchestrator(
        store=store, llm=llm, tools=tools, max_tool_attempts=2, retry_base_delay_seconds=0
    ).run(run_id)

    # Retried to budget, given up on, and the sibling still ran.
    assert attempts == ["x", "x", "weather:Lima"]
    assert state.status == "completed"

    types = [type(e).__name__ for e in await store.read(run_id)]
    assert types.count("ToolCallFailed") == 2
    assert types.count("ToolCallCompleted") == 1


@pytest.mark.asyncio
async def test_retry_inside_a_batch_reuses_the_same_idempotency_key() -> None:
    """The exactly-once guarantee has to survive batching: a call that
    fails once and succeeds on retry must present one key, not two.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    keys: list[str] = []

    @tool(side_effect=True)
    async def charge(amount: int, idempotency_key: str) -> dict[str, Any]:
        keys.append(idempotency_key)
        if len(keys) == 1:
            raise RuntimeError("payments API timed out")
        return {"charge_id": "CH-1", "amount": amount}

    @tool()
    async def get_weather(city: str) -> dict[str, Any]:
        return {"city": city, "temp_c": 20}

    tools = {t.name: t for t in (charge, get_weather)}

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Charge and check.",
                tool_calls=[
                    ToolCallInvocation(id="p1", name="charge", arguments={"amount": 100}),
                    ToolCallInvocation(id="c1", name="get_weather", arguments={"city": "Oslo"}),
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="Charged."),
        ]
    )
    state = await Orchestrator(store=store, llm=llm, tools=tools, retry_base_delay_seconds=0).run(
        run_id
    )

    assert state.status == "completed"
    assert len(keys) == 2, "the call was attempted twice"
    assert keys[0] == keys[1], "same key both times, or a backend double-charges"


@pytest.mark.asyncio
async def test_hallucinated_tool_in_a_batch_does_not_stall_it() -> None:
    """An unknown tool name fails before any ToolCallRequested is written
    — a different code path from a tool that raises — and still has to
    mark that call answered.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())
    tools, executed = _tools()

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Two things.",
                tool_calls=[
                    ToolCallInvocation(id="x1", name="not_a_real_tool", arguments={}),
                    ToolCallInvocation(id="c1", name="get_weather", arguments={"city": "Paris"}),
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="One tool did not exist; Paris is 20C."),
        ]
    )
    state = await Orchestrator(store=store, llm=llm, tools=tools).run(run_id)

    assert executed == ["weather:Paris"]
    assert state.status == "completed"


@pytest.mark.asyncio
async def test_duplicate_ids_from_a_misbehaving_model_still_terminate() -> None:
    """Nothing stops a provider repeating an id. Matching clears every
    call sharing it, so the batch finishes instead of looping — the
    property that matters when the input is malformed.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())
    tools, executed = _tools()

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Two, confusingly labelled.",
                tool_calls=[
                    ToolCallInvocation(id="same", name="get_weather", arguments={"city": "Paris"}),
                    ToolCallInvocation(id="same", name="get_weather", arguments={"city": "Tokyo"}),
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="Done."),
        ]
    )
    state = await Orchestrator(store=store, llm=llm, tools=tools).run(run_id)

    assert state.status == "completed"
    # One result answers both, which is the honest reading of an
    # ambiguous id. Termination is the property under test.
    assert executed == ["weather:Paris"]


@pytest.mark.asyncio
async def test_a_batch_is_one_step_not_several() -> None:
    """Step caps count model turns, not tool calls. A wide batch must not
    burn the step budget, or a run doing three lookups at once would trip
    a cap sized for three reasoning steps.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())
    tools, executed = _tools()

    llm = ScriptedLLM([_three_city_call(), _llm_response(content="All 20C.")])
    state = await Orchestrator(store=store, llm=llm, tools=tools).run(run_id)

    assert len(executed) == 3
    assert state.status == "completed"
    assert state.step == 2, "two model turns, however many tools they asked for"


# --- batches of genuinely different tools -------------------------------
#
# Everything above calls one or two distinct tools. The realistic case is
# a fan-out across several unrelated systems, where each call validates
# against its own args schema, only some tools take an idempotency key,
# and only some need a human.


def _offboarding_tools() -> tuple[dict[str, Tool], list[str]]:
    done: list[str] = []

    @tool(side_effect=True)
    async def revoke_okta(user: str, idempotency_key: str) -> dict[str, Any]:
        """Revoke a user's Okta session."""
        done.append(f"okta:{user}:{idempotency_key[:8]}")
        return {"user": user, "revoked": True}

    @tool(side_effect=True)
    async def revoke_github(user: str, org: str) -> dict[str, Any]:
        """Remove a user from a GitHub org. Takes no idempotency key."""
        done.append(f"github:{user}@{org}")
        return {"user": user, "org": org, "removed": True}

    @tool()
    async def lookup_manager(user: str) -> dict[str, Any]:
        """Read-only: who does this person report to."""
        done.append(f"manager:{user}")
        return {"user": user, "manager": "rex"}

    return {t.name: t for t in (revoke_okta, revoke_github, lookup_manager)}, done


@pytest.mark.asyncio
async def test_three_different_tools_in_one_turn_all_execute() -> None:
    """The realistic fan-out: three unrelated tools, three different
    argument schemas, only one of which declares an idempotency_key.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())
    tools, done = _offboarding_tools()

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Revoking access and finding the manager.",
                tool_calls=[
                    ToolCallInvocation(id="o1", name="revoke_okta", arguments={"user": "dana"}),
                    ToolCallInvocation(
                        id="g1", name="revoke_github", arguments={"user": "dana", "org": "acme"}
                    ),
                    ToolCallInvocation(id="m1", name="lookup_manager", arguments={"user": "dana"}),
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="Dana is offboarded; manager is rex."),
        ]
    )
    state = await Orchestrator(store=store, llm=llm, tools=tools).run(run_id)

    assert state.status == "completed"
    assert [d.split(":")[0] for d in done] == ["okta", "github", "manager"]

    # Only revoke_okta declares idempotency_key, so only it receives one —
    # the injection is per tool, not per batch.
    assert done[0].startswith("okta:dana:") and len(done[0].split(":")[2]) == 8
    assert done[1] == "github:dana@acme"

    events = await store.read(run_id)
    requested = [e for e in events if isinstance(e, ToolCallRequested)]
    assert [e.tool for e in requested] == ["revoke_okta", "revoke_github", "lookup_manager"]
    assert [e.tool_call_id for e in requested] == ["o1", "g1", "m1"]
    # Each call is its own side effect and must not share a key with a sibling.
    assert len({e.idempotency_key for e in requested}) == 3

    # Every call answered, each against its own id, so the next request
    # to a provider is well formed.
    client = OpenAICompatibleClient(base_url="http://example.invalid/v1", model="m")
    wire = client._to_openai_messages(rebuild_state(events).messages, "")
    assert sorted(m["tool_call_id"] for m in wire if m["role"] == "tool") == ["g1", "m1", "o1"]


@pytest.mark.asyncio
async def test_approval_in_the_middle_of_a_mixed_batch() -> None:
    """Only the second of three different tools needs a human. The first
    must already have run, the approval must release only that one, and
    the third must still run afterwards.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    done: list[str] = []

    @tool()
    async def lookup_manager(user: str) -> dict[str, Any]:
        """Read-only."""
        done.append(f"manager:{user}")
        return {"user": user, "manager": "rex"}

    @tool(requires_approval=True, side_effect=True)
    async def wipe_laptop(asset_tag: str) -> dict[str, Any]:
        """Destructive: needs a human."""
        done.append(f"wipe:{asset_tag}")
        return {"asset_tag": asset_tag, "wiped": True}

    @tool(side_effect=True)
    async def revoke_github(user: str, org: str) -> dict[str, Any]:
        """No approval needed."""
        done.append(f"github:{user}@{org}")
        return {"user": user, "removed": True}

    tools = {t.name: t for t in (lookup_manager, wipe_laptop, revoke_github)}

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Offboarding dana.",
                tool_calls=[
                    ToolCallInvocation(id="m1", name="lookup_manager", arguments={"user": "dana"}),
                    ToolCallInvocation(
                        id="w1", name="wipe_laptop", arguments={"asset_tag": "MBP-1"}
                    ),
                    ToolCallInvocation(
                        id="g1", name="revoke_github", arguments={"user": "dana", "org": "acme"}
                    ),
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="Dana is fully offboarded."),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=llm, tools=tools)

    state = await orchestrator.run(run_id)

    # Parked on the middle call, with the read-only one already done and
    # the third untouched — a human is asked about exactly one thing.
    assert state.status == "awaiting_approval"
    assert state.pending_approval is not None
    assert state.pending_approval.tool == "wipe_laptop"
    assert state.pending_approval.tool_call_id == "w1"
    assert done == ["manager:dana"]

    events = await store.read(run_id)
    await store.append(
        run_id, len(events), ApprovalGranted(seq=len(events), created_at=_now(), approver="dana.m")
    )

    state = await orchestrator.run(run_id)

    assert state.status == "completed"
    assert done == ["manager:dana", "wipe:MBP-1", "github:dana@acme"]
    # The read-only call ran once, not again on resume.
    assert done.count("manager:dana") == 1
