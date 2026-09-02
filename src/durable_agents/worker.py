import asyncio
import logging
from uuid import UUID

from durable_agents.runtime import Runtime

logger = logging.getLogger(__name__)


class Worker:
    """Polls for runs that need work and resumes them.

    This is what turns durability into recovery. Without it, a run whose
    process died sits in the log forever until a human notices and calls
    resume() by hand — the log is durable, but nothing is self-healing.
    With a Worker running, a process can be killed mid-tool-call and
    something else picks the run up and finishes it, with the
    idempotency keys already in the log making sure no side effect
    happens twice.

    Spec describes a "worker" and a "recovery sweeper" as two separate
    processes. They are the same mechanism — find a run_id, call
    resume() — differing only in how long a run must be quiet before
    it's considered abandoned, so this is one class with that duration
    as a parameter. Run one instance and it does both jobs; run two with
    different thresholds if you want spec's split literally.

    Nothing here is specific to any agent: a Worker takes a Runtime,
    which already carries the tools and LLM client the consumer
    configured.
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        stale_after_seconds: float = 60.0,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 10,
    ) -> None:
        self._runtime = runtime
        self._stale_after_seconds = stale_after_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size

    async def poll_once(self) -> list[UUID]:
        """One pass: find resumable runs, resume each, return what was
        worked on.

        Separate from run_forever() so tests (and anything wanting its
        own scheduling) can drive a single pass without an infinite
        loop.
        """

        run_ids = await self._runtime.store.find_resumable_runs(
            stale_after_seconds=self._stale_after_seconds, limit=self._batch_size
        )

        worked: list[UUID] = []
        for run_id in run_ids:
            try:
                state = await self._runtime.resume(run_id)
            except Exception:
                # One poisoned run must not take the worker down with
                # it — that would turn a single bad run into an outage
                # for every other run in the system. The failure is
                # logged and the loop moves on; the run stays resumable
                # and will be retried on a later pass.
                logger.exception("worker failed while resuming run %s", run_id)
                continue
            worked.append(run_id)
            logger.info("worker advanced run %s to status=%s", run_id, state.status)

        return worked

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        """Poll until `stop` is set (or forever, if none is given).

        Waits on the stop event rather than sleeping blindly, so
        shutdown is immediate instead of taking up to a full poll
        interval.
        """

        stop = stop or asyncio.Event()
        logger.info(
            "worker started (poll every %.1fs, treating runs quiet for %.1fs as abandoned)",
            self._poll_interval_seconds,
            self._stale_after_seconds,
        )
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                # Covers failures of the polling query itself (e.g. the
                # database going away), as opposed to a single run
                # failing, which poll_once already handles. Keep
                # looping so the worker recovers when the database
                # comes back rather than needing a restart.
                logger.exception("worker poll failed; continuing")

            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass

        logger.info("worker stopped")
