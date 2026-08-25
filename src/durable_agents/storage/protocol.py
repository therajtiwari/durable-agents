from abc import ABC, abstractmethod
from uuid import UUID

from durable_agents.events import Event


class ConcurrencyConflict(Exception):
    """Raised by EventStore.append when expected_seq is already taken."""


class EventStore(ABC):
    @abstractmethod
    async def append(self, run_id: UUID, expected_seq: int, event: Event) -> None:
        """Append event at expected_seq. Raises ConcurrencyConflict if taken."""
        raise NotImplementedError

    @abstractmethod
    async def read(self, run_id: UUID) -> list[Event]:
        """Read all events for run_id, ordered by seq."""
        raise NotImplementedError

    @abstractmethod
    async def read_since(self, run_id: UUID, seq: int) -> list[Event]:
        """Read events for run_id with seq strictly greater than seq, ordered by seq."""
        raise NotImplementedError
