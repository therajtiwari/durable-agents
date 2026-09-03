import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, create_model


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    side_effect: bool
    requires_approval: Callable[[dict[str, Any]], bool]
    func: Callable[..., Awaitable[Any]]
    needs_idempotency_key: bool
    args_model: type[BaseModel]
    """The same Pydantic model _build_parameters_model derives `parameters`
    (the JSON schema) from — kept around so L3 output validation
    (guardrails/output_validate.py) can re-validate a live tool call's
    arguments with model_validate() instead of re-deriving a second
    schema-checking path from scratch.
    """

    async def execute(self, **kwargs: Any) -> Any:
        return await self.func(**kwargs)


def _build_parameters_model(func: Callable[..., Awaitable[Any]]) -> type[BaseModel]:
    """Auto-derive a Pydantic model for func's parameters from its type
    hints. Excludes a parameter literally named idempotency_key: that
    value is computed and injected by the orchestrator, never something
    the model should see as a field to fill in or invent.

    Rejects signatures that cannot produce a usable JSON schema. All of
    these previously succeeded and shipped something broken to the
    provider, which is a much worse failure than a loud one here: the
    tool looks registered, the model is handed nonsense, and the mistake
    surfaces as a confusing provider error or a hallucinated argument
    mid-run.
    """

    signature = inspect.signature(func)
    fields: dict[str, Any] = {}
    for name, param in signature.parameters.items():
        if name == "idempotency_key":
            continue

        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            # *args became a single scalar field named "args" and
            # **kwargs one named "kwargs" — schemas the model would
            # dutifully try to fill with a value that means nothing.
            raise ValueError(
                f"tool {func.__name__!r} cannot use *{name}: a tool's arguments have to be "
                f"named for the model to fill them in. Declare each parameter explicitly."
            )

        if name.startswith("model_"):
            # create_model treats these as Pydantic's own configuration,
            # and model_config in particular failed with
            # "TypeError: 'ellipsis' object is not iterable" — an error
            # naming nothing the caller wrote.
            raise ValueError(
                f"tool {func.__name__!r} cannot have a parameter named {name!r}: names "
                f"beginning with 'model_' are reserved by Pydantic. Rename the parameter."
            )

        if param.annotation is inspect.Parameter.empty:
            # Produced {"title": "X"} with no "type" at all. Some
            # providers reject a typeless property outright; the rest
            # leave the model guessing.
            raise ValueError(
                f"tool {func.__name__!r} is missing a type annotation for {name!r}. The "
                f"parameter's type is what tells the model what to send."
            )

        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[name] = (param.annotation, default)

    # extra="forbid" so an argument the function does not accept is caught
    # by validation instead of at call time. It used to reach
    # execute(**kwargs), raise TypeError("unexpected keyword argument"),
    # and then be retried the full three times with backoff — a
    # deterministic failure treated as a transient one. Retrying every
    # exception is right for provider errors, but a wrong keyword is
    # knowably not one.
    #
    # It also puts "additionalProperties": false in the JSON schema,
    # which tells the provider not to invent fields in the first place.
    model: type[BaseModel] = create_model(
        func.__name__, __config__=ConfigDict(extra="forbid"), **fields
    )
    return model


def _build_parameters_schema(model: type[BaseModel]) -> dict[str, Any]:
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
        args_model = _build_parameters_model(func)
        return Tool(
            name=func.__name__,
            description=inspect.getdoc(func) or "",
            parameters=_build_parameters_schema(args_model),
            side_effect=side_effect,
            requires_approval=approval_check,
            func=func,
            needs_idempotency_key=needs_idempotency_key,
            args_model=args_model,
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
