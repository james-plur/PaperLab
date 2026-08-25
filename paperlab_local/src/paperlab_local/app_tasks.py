"""In-memory task registry used by the desktop application."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass
class AppTask:
    id: str
    kind: str
    label: str
    status: str = "queued"
    progress: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: Any = None
    error: str | None = None
    cancel_requested: bool = False

    def public(self) -> dict:
        value = asdict(self)
        value.pop("cancel_requested", None)
        return value


class TaskRegistry:
    def __init__(self):
        self._tasks: dict[str, AppTask] = {}
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)
            return [task.public() for task in tasks]

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.public() if task else None

    def cancel(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            if task.status in {"queued", "running"}:
                task.cancel_requested = True
                if task.status == "queued":
                    task.status = "cancelled"
                task.updated_at = time.time()
            return task.public()

    def submit(self, kind: str, label: str, work: Callable[[Callable[[float], None], Callable[[], bool]], Any]) -> dict:
        task = AppTask(id=str(uuid.uuid4()), kind=kind, label=label)
        with self._lock:
            self._tasks[task.id] = task

        def update(progress: float) -> None:
            with self._lock:
                task.progress = max(0.0, min(1.0, float(progress)))
                task.updated_at = time.time()

        def cancelled() -> bool:
            with self._lock:
                return task.cancel_requested

        def run() -> None:
            with self._lock:
                if task.cancel_requested:
                    task.status = "cancelled"
                    return
                task.status = "running"
                task.updated_at = time.time()
            try:
                result = work(update, cancelled)
                with self._lock:
                    task.result = result
                    task.progress = 1.0
                    task.status = "cancelled" if task.cancel_requested else "completed"
                    task.updated_at = time.time()
            except Exception as exc:
                with self._lock:
                    task.status = "failed"
                    task.error = str(exc)[:1000]
                    task.updated_at = time.time()

        threading.Thread(target=run, name=f"paperlab-{kind}-{task.id[:8]}", daemon=True).start()
        return task.public()


TASKS = TaskRegistry()
