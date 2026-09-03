"""The refund scenario this project demonstrates itself with.

Deliberately outside `src/durable_agents/`, so none of it ships in the
wheel. It is application code written *against* the runtime, not part of
it: a consumer installing durable-agents wants the `@tool` decorator and
the orchestrator, not somebody else's refund tools.

It lives here rather than under `tests/` because both the test suite and
the example scripts need it, and an example importing from a test
directory would be backwards.

Importable two ways, both without installation:

- example scripts run as `python examples/foo.py`, which puts
  `examples/` on `sys.path` automatically
- tests, via `pythonpath = ["examples"]` in pyproject's pytest config

Why refunds: the point of the project is that a crashed run does not
repeat side effects, and issuing a refund twice is *visibly* wrong in a
way that "a file written twice" is not. The demo assertion is two
attempts, one refund.
"""

from .postgres_backend import PostgresRefundBackend
from .scenario import (
    canonical_run_started,
    canonical_script,
    parallel_refund_script,
    parallel_run_started,
)
from .tools import InMemoryRefundBackend, RefundBackend, build_refund_tools

__all__ = [
    "InMemoryRefundBackend",
    "PostgresRefundBackend",
    "RefundBackend",
    "build_refund_tools",
    "canonical_run_started",
    "canonical_script",
    "parallel_refund_script",
    "parallel_run_started",
]
