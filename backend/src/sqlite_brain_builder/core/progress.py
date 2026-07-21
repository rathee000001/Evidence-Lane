from __future__ import annotations
from dataclasses import dataclass, asdict
import time
from datetime import datetime, timedelta

@dataclass
class ProgressState:
    total_units: int = 1
    completed_units: int = 0
    percent: float = 0.0
    stage: str = "idle"
    current_task: str = ""
    current_file: str = ""
    start_time: float = 0.0
    elapsed_seconds: float = 0.0
    eta_seconds: float | None = None
    estimated_finish_local: str = ""
    chunks_written: int = 0
    warnings_count: int = 0
    review_required_count: int = 0
    cancel_requested: bool = False

class ProgressTracker:
    def __init__(self, total_units: int = 1):
        self.state = ProgressState(total_units=max(1,total_units), start_time=time.time(), stage="start")

    def set_total(self, total_units: int):
        self.state.total_units = max(1,total_units)
        self._recalc()

    def update(self, completed: int | None = None, stage: str | None = None, task: str | None = None, file: str | None = None, chunks: int = 0, warnings: int = 0, review: int = 0):
        if completed is not None:
            self.state.completed_units = max(self.state.completed_units, min(completed, self.state.total_units))
        if stage is not None: self.state.stage = stage
        if task is not None: self.state.current_task = task
        if file is not None: self.state.current_file = file
        self.state.chunks_written += chunks
        self.state.warnings_count += warnings
        self.state.review_required_count += review
        self._recalc()
        return self.snapshot()

    def step(self, stage: str, task: str, file: str = "", chunks: int = 0, warnings: int = 0, review: int = 0):
        return self.update(self.state.completed_units+1, stage, task, file, chunks, warnings, review)

    def request_cancel(self):
        self.state.cancel_requested = True
        self._recalc()

    def _recalc(self):
        self.state.elapsed_seconds = max(0.0, time.time() - self.state.start_time)
        self.state.percent = round(100.0 * self.state.completed_units / max(1,self.state.total_units), 2)
        if self.state.completed_units > 0 and self.state.completed_units < self.state.total_units:
            self.state.eta_seconds = self.state.elapsed_seconds * (self.state.total_units - self.state.completed_units) / max(self.state.completed_units, 1)
            self.state.estimated_finish_local = (datetime.now() + timedelta(seconds=self.state.eta_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        elif self.state.completed_units >= self.state.total_units:
            self.state.eta_seconds = 0.0
            self.state.estimated_finish_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def snapshot(self) -> dict:
        return asdict(self.state)
