import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import create_model


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    side_effect: bool
    requires_approval: Callable[[dict[str, Any]], bool]
    func: Callable[..., Awaitable[Any]]
    needs_idempotency_key: bool

    async def execute(self, **kwargs: Any) -> Any:
        return await self.func(**kwargs)


def _build_parameters_schema(func: Callable[..., Awaitable[Any]]) -> dict[str, Any]:
    """Auto-derive a JSON schema for func's parameters from its type hints.

    Excludes a parameter literally named idempotency_key: that value is
    computed and injected by the orchestrator, never something the model
    should see as a field to fill in or invent.
    """

    signature = inspect.signature(func)
    fields: dict[str, Any] = {}
    for name, param in signature.parameters.items():
        if name == "idempotency_key":
            continue
        annotation = param.annotation if param.annotation is not inspect.Parameter.empty else Any
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[name] = (annotation, default)

    model = create_model(func.__name__, **fields)
    schema: dict[str, Any] = model.model_json_schema()
    schema.pop("title", None)
    return schema


def tool(
    *,
    requires_approval: bool | Callable[[dict[str, Any]], bool] = False,
    side_effect: bool = False,
) -> Callable[[Callable[..., Awaitable[Any]]], Tool]:
    """Turn an async function into a Tool the orchestrator can call.

    requires_approval may be a plain bool (always/never needs approval) or
    a predicate over the call arguments (e.g. only above a threshold) — it
    is normalized here into a callable either way, so calling code never
    needs to check which form was originally passed.
    """

    approval_check: Callable[[dict[str, Any]], bool]
    if isinstance(requires_approval, bool):
        approval_check = lambda _args: requires_approval  # noqa: E731
    else:
        approval_check = requires_approval

    def decorator(func: Callable[..., Awaitable[Any]]) -> Tool:
        needs_idempotency_key = "idempotency_key" in inspect.signature(func).parameters
        return Tool(
            name=func.__name__,
            description=inspect.getdoc(func) or "",
            parameters=_build_parameters_schema(func),
            side_effect=side_effect,
            requires_approval=approval_check,
            func=func,
            needs_idempotency_key=needs_idempotency_key,
        )

    return decorator


def idempotency_key(run_id: UUID, seq: int, tool_name: str, arguments: dict[str, Any]) -> str:
    """sha256(run_id + seq + tool_name + canonical_json(args)).

    Deterministic: the same logical step always produces the same key, no
    matter how many times the process restarts.
    """

    canonical_args = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    payload = f"{run_id}:{seq}:{tool_name}:{canonical_args}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
