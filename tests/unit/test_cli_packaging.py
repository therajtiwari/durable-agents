"""What `pip install durable-agents` actually gives someone.

The console script is the entry point every doc points at, and it was
broken on a plain install: cli.py imported OpenAICompatibleClient at
module scope, which imports httpx, which lives in the optional "openai"
extra — so `durable-agents --help` died with ModuleNotFoundError for
anyone who had not also installed that extra. A full green test suite
said nothing about it, because the dev environment has httpx.

These tests check the import graph rather than behaviour, since that is
where the defect lives.
"""

import subprocess
import sys

import pytest

from durable_agents.cli import redact_dsn


def _imports_after(module: str) -> set[str]:
    """Modules present in sys.modules after importing `module`, in a
    fresh interpreter so nothing another test imported can mask a
    missing dependency.
    """

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys, {module}; print('\\n'.join(sorted(sys.modules)))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.split())


@pytest.mark.parametrize("optional", ["httpx", "fastapi", "uvicorn"])
def test_cli_does_not_import_optional_extras_at_module_scope(optional: str) -> None:
    """Importing the CLI must not drag in anything from an extra. Each
    of these is optional on purpose, and pulling one in at module scope
    turns `pip install durable-agents` into a broken console script.
    """

    assert optional not in _imports_after("durable_agents.cli")


@pytest.mark.parametrize("demo", ["refund_tools", "refund_demo_scenario", "refund_backend_postgres"])
def test_cli_does_not_import_demo_modules_at_module_scope(demo: str) -> None:
    """The refund modules are demo content, due to move out of the
    shipped package. Importing them at module scope would take the whole
    CLI down with them when they go — including `replay`, which has
    nothing to do with the demo.
    """

    assert f"durable_agents.tools.{demo}" not in _imports_after("durable_agents.cli")


def test_importing_the_package_does_not_require_any_extra() -> None:
    for optional in ("httpx", "fastapi", "uvicorn"):
        assert optional not in _imports_after("durable_agents")


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents",
            "postgresql://durable_agents:***@localhost:5432/durable_agents",
        ),
        (
            "postgresql://user:hunter2@db.example.com:5432/prod?sslmode=require",
            "postgresql://user:***@db.example.com:5432/prod?sslmode=require",
        ),
        # Nothing to hide: left exactly as given.
        ("postgresql://localhost:5432/nopassword", "postgresql://localhost:5432/nopassword"),
        ("postgresql://user@localhost/nouserpass", "postgresql://user@localhost/nouserpass"),
    ],
)
def test_redact_dsn_keeps_the_useful_part_and_hides_the_password(dsn: str, expected: str) -> None:
    assert redact_dsn(dsn) == expected


def test_redact_dsn_never_echoes_a_password_it_could_not_parse() -> None:
    """An unparseable string drops everything before the "@" rather than
    guessing at its structure — printing the host is a convenience, and
    not leaking the password is the point.
    """

    assert "hunter2" not in redact_dsn("postgres//user:hunter2@host/db")
