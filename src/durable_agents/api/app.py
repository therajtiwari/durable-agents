from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from durable_agents.events import ApprovalDenied, ApprovalGranted
from durable_agents.state import RunState, RunStatus, rebuild_state
from durable_agents.storage.protocol import ConcurrencyConflict, EventStore


class PendingApprovalResponse(BaseModel):
    tool: str
    arguments: dict[str, Any]
    reason: str


class RunStatusResponse(BaseModel):
    run_id: UUID
    status: RunStatus
    step: int
    total_tokens: int
    total_cost_usd: Decimal
    pending_approval: PendingApprovalResponse | None
    final_answer: str | None
    failure_reason: str | None


class ApproveRequest(BaseModel):
    approver: str


class DenyRequest(BaseModel):
    approver: str
    reason: str


def get_store(request: Request) -> EventStore:
    return request.app.state.store  # type: ignore[no-any-return]


async def _load_state(run_id: UUID, store: EventStore) -> tuple[RunState, int]:
    """Returns the rebuilt state plus the event count (== next expected seq)."""
    events = await store.read(run_id)
    if not events:
        raise HTTPException(status_code=404, detail="run not found")
    return rebuild_state(events), len(events)


def _status_response(run_id: UUID, state: RunState) -> RunStatusResponse:
    return RunStatusResponse(
        run_id=run_id,
        status=state.status,
        step=state.step,
        total_tokens=state.total_tokens,
        total_cost_usd=state.total_cost_usd,
        pending_approval=(
            PendingApprovalResponse(
                tool=state.pending_approval.tool,
                arguments=state.pending_approval.arguments,
                reason=state.pending_approval.reason,
            )
            if state.pending_approval is not None
            else None
        ),
        final_answer=state.final_answer,
        failure_reason=state.failure_reason,
    )


def create_app(store: EventStore) -> FastAPI:
    app = FastAPI(title="durable-agents")
    app.state.store = store

    @app.get("/runs/{run_id}", response_model=RunStatusResponse)
    async def get_run_status(
        run_id: UUID, store: EventStore = Depends(get_store)
    ) -> RunStatusResponse:
        state, _next_seq = await _load_state(run_id, store)
        return _status_response(run_id, state)

    @app.post("/runs/{run_id}/approve", status_code=204)
    async def approve_run(
        run_id: UUID, body: ApproveRequest, store: EventStore = Depends(get_store)
    ) -> None:
        state, next_seq = await _load_state(run_id, store)
        if state.status != "awaiting_approval":
            raise HTTPException(
                status_code=409, detail=f"run is not awaiting approval (status={state.status})"
            )
        try:
            await store.append(
                run_id,
                next_seq,
                ApprovalGranted(
                    seq=next_seq, created_at=datetime.now(timezone.utc), approver=body.approver
                ),
            )
        except ConcurrencyConflict as exc:
            raise HTTPException(
                status_code=409, detail="run state changed concurrently, retry"
            ) from exc

    @app.post("/runs/{run_id}/deny", status_code=204)
    async def deny_run(
        run_id: UUID, body: DenyRequest, store: EventStore = Depends(get_store)
    ) -> None:
        state, next_seq = await _load_state(run_id, store)
        if state.status != "awaiting_approval":
            raise HTTPException(
                status_code=409, detail=f"run is not awaiting approval (status={state.status})"
            )
        try:
            await store.append(
                run_id,
                next_seq,
                ApprovalDenied(
                    seq=next_seq,
                    created_at=datetime.now(timezone.utc),
                    approver=body.approver,
                    reason=body.reason,
                ),
            )
        except ConcurrencyConflict as exc:
            raise HTTPException(
                status_code=409, detail="run state changed concurrently, retry"
            ) from exc

    return app
