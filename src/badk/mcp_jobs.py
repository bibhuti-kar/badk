"""`badk mcp` jobs (design 28.3): one run at a time on the main thread, start + poll."""

import asyncio
import queue
import signal
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

from badk.errors import BadkError, Code, redact

WAIT_MAX = 45
RunFn = Callable[..., Any]  # badk.run: (ask, workflow=, agents_dir=, on_run_id=) -> RunResult


@dataclass
class Job:
    job_id: str
    ask: str
    workflow: str | None
    future: "Future[dict[str, Any]]" = field(default_factory=Future)
    run_id: str | None = None
    started: bool = False


class Jobs:
    """Job table and queue. `work()` runs on the main thread (proc cleanup needs it)."""

    def __init__(self, run_fn: RunFn, agents_dir: str, viewer_url: str) -> None:
        self._run, self._dir, self._viewer = run_fn, agents_dir, viewer_url
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[Job | None] = queue.Queue()
        # Guards _jobs, _queue drain and the _active/_closed pair: close() and work() decide
        # "interrupt the run" vs "never start it" under it, so no job slips between the two.
        # Job.started/run_id are single writes by the main thread, read by the server thread.
        self._lock = threading.Lock()
        self._active = False
        self._closed = False

    def submit(self, ask: str, workflow: str | None) -> Job:
        job = Job(uuid.uuid4().hex[:12], ask, workflow)
        with self._lock:
            self._jobs[job.job_id] = job
            if self._closed:
                job.future.set_result(self._failed(job, "SHUTDOWN", "server is closing"))
            else:
                self._queue.put(job)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ValueError(f"unknown job_id: {job_id}")
        return job

    def _viewer_of(self, job: Job) -> str | None:
        return f"{self._viewer}/runs/{job.run_id}" if job.run_id else None

    def _failed(self, job: Job, code: str, message: str) -> dict[str, Any]:
        out: dict[str, Any] = {"job_id": job.job_id, "status": "FAILED", "answer": ""}
        out["error"] = {"code": code, "message": redact(message)}
        if job.run_id:
            out |= {"run_id": job.run_id, "viewer": self._viewer_of(job)}
        return out

    def _execute(self, job: Job) -> dict[str, Any]:
        try:
            res = self._run(
                job.ask,
                workflow=job.workflow,
                agents_dir=self._dir,
                on_run_id=lambda rid: setattr(job, "run_id", rid),
            )
        except BadkError as e:
            job.run_id = job.run_id or e.run_id
            failed = self._failed(job, e.code.value, e.message)
            failed["answer"] = e.answer or ""
            return failed
        except Exception as e:  # MCP-3.5: one failed run never ends the server
            return self._failed(job, Code.INTERNAL_ERROR.value, f"{type(e).__name__}: {e}")
        out: dict[str, Any] = {"job_id": job.job_id, "run_id": res.run_id, "status": res.status}
        out |= {"answer": res.answer, "viewer": res.url}
        if res.error:
            out["error"] = {"code": res.error.code.value, "message": redact(res.error.message)}
        return out

    def work(self) -> None:
        """Main-thread loop: run queued jobs in order until `close()`; a Ctrl-C ends it."""
        try:
            while (job := self._queue.get()) is not None:
                with self._lock:
                    if self._closed:  # close() ran after the get: this job never starts
                        job.future.set_result(self._failed(job, "SHUTDOWN", "server closed"))
                        break
                    self._active = job.started = True
                try:
                    job.future.set_result(self._execute(job))
                finally:
                    with self._lock:
                        self._active = False
        except KeyboardInterrupt:  # close() interrupted the active run (NC-33 path)
            pass

    def close(self) -> None:
        """Server thread ended: queued jobs never start; an active run is interrupted."""
        with self._lock:
            self._closed = True
            while True:
                try:
                    job = self._queue.get_nowait()
                except queue.Empty:
                    break
                if job is not None:
                    job.future.set_result(self._failed(job, "SHUTDOWN", "server closed"))
            self._queue.put(None)
            if self._active:
                # a real SIGINT to the main thread (interrupt_main would not wake a blocked wait)
                signal.pthread_kill(threading.main_thread().ident or 0, signal.SIGINT)

    async def wait(self, job: Job, sec: int) -> dict[str, Any]:
        """Done shape, or the QUEUED/RUNNING shape after `sec` s. Never cancels the job."""
        if not job.future.done():
            await asyncio.wait({asyncio.wrap_future(job.future)}, timeout=sec)
        if job.future.done():
            return job.future.result()
        out: dict[str, Any] = {"job_id": job.job_id}
        out["status"] = "RUNNING" if job.started else "QUEUED"
        if job.run_id:
            out |= {"run_id": job.run_id, "viewer": self._viewer_of(job)}
        return out
