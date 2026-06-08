"""
Automation status file manager.

Each automation writes logs/{name}_status.json while running.
The Jarvis dashboard reads these to show live progress without
coupling the UI to any automation's internals.

Status lifecycle:  idle → starting → running → completed | failed

Usage (in an automation):
    from src.automation_status import StatusWriter
    with StatusWriter("competitor_analysis", total=len(products)) as sw:
        for i, product in enumerate(products):
            sw.tick(done=i+1, current=product["sku"], stage="Scraping")
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).parent.parent
LOGS_DIR   = _REPO_ROOT / "logs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(
    automation: str,
    *,
    status: str,
    stage: str        = "",
    total: int        = 0,
    done: int         = 0,
    current: str      = "",
    error: str        = "",
    started_at: str   = "",
) -> None:
    """Write (or overwrite) the status file for an automation."""
    LOGS_DIR.mkdir(exist_ok=True)
    now = _now()
    data = {
        "automation":   automation,
        "status":       status,           # idle | starting | running | completed | failed
        "pid":          os.getpid(),
        "started_at":   started_at or now,
        "stage":        stage,
        "total":        total,
        "done":         done,
        "current":      current,
        "last_updated": now,
        "completed_at": now if status in ("completed", "failed") else None,
        "error":        error,
    }
    path = LOGS_DIR / f"{automation}_status.json"
    # Write atomically-ish: write to .tmp then rename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists (signal 0 = check only)."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False
    except Exception:
        return False


def read_status(automation: str) -> dict:
    """
    Read the current status for an automation. Returns idle if no file.

    Stale-PID guard: if the file claims status='running' but the recorded
    PID is no longer alive (process crashed, killed, OS restarted), we
    flip the in-memory status to 'failed' so the UI can recover instead
    of showing a perpetual progress bar. The file on disk is NOT mutated
    — the next successful write supersedes it.
    """
    path = LOGS_DIR / f"{automation}_status.json"
    if not path.exists():
        return {"automation": automation, "status": "idle", "total": 0, "done": 0}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"automation": automation, "status": "idle", "total": 0, "done": 0}

    # Stale-PID detection
    if data.get("status") in ("running", "starting"):
        if not _pid_alive(data.get("pid", 0)):
            data = {
                **data,
                "status": "failed",
                "error": data.get("error") or "Process died without writing a completion status",
            }
    return data


# ---------------------------------------------------------------------------
# Lockfile — prevents two concurrent runs of the same automation
# ---------------------------------------------------------------------------

class AutomationLockError(RuntimeError):
    """Raised when an automation lock cannot be acquired."""


def acquire_lock(automation: str) -> Path:
    """
    Acquire an exclusive lock for this automation. Writes our PID to
    logs/{automation}.lock. If a lock file already exists AND its PID is
    alive, raises AutomationLockError. Stale locks (dead PID) are reclaimed.

    Returns the lock path so the caller can release it later.
    """
    LOGS_DIR.mkdir(exist_ok=True)
    lock = LOGS_DIR / f"{automation}.lock"
    if lock.exists():
        try:
            other_pid = int(lock.read_text().strip() or "0")
        except Exception:
            other_pid = 0
        if other_pid and _pid_alive(other_pid):
            raise AutomationLockError(
                f"{automation} is already running (PID {other_pid}). "
                f"Wait for it to finish or kill it first."
            )
        # Stale — reclaim
    lock.write_text(str(os.getpid()))
    return lock


def release_lock(automation: str) -> None:
    """Remove the lockfile (idempotent — safe to call even if no lock exists)."""
    lock = LOGS_DIR / f"{automation}.lock"
    try:
        if lock.exists():
            # Only delete if it's OUR lock (PID matches)
            try:
                owner_pid = int(lock.read_text().strip() or "0")
            except Exception:
                owner_pid = 0
            if owner_pid in (0, os.getpid()):
                lock.unlink()
    except Exception:
        pass


class StatusWriter:
    """
    Context manager that handles start/complete/fail lifecycle automatically.

    with StatusWriter("competitor_analysis", total=48) as sw:
        for i, product in enumerate(products):
            sw.tick(done=i+1, current=sku, stage="Scraping competitors")
    """

    def __init__(self, automation: str, *, total: int = 0, stage: str = "") -> None:
        self.automation = automation
        self.total      = total
        self._started   = _now()
        self._stage     = stage

    def __enter__(self) -> "StatusWriter":
        # Acquire lock first — raises AutomationLockError if another instance
        # is already running (and its PID is alive).
        acquire_lock(self.automation)
        write_status(
            self.automation,
            status="starting",
            stage=self._stage or "Initialising",
            total=self.total,
            done=0,
            started_at=self._started,
        )
        return self

    def tick(
        self,
        *,
        done: int,
        current: str = "",
        stage: str   = "",
    ) -> None:
        if stage:
            self._stage = stage
        write_status(
            self.automation,
            status="running",
            stage=self._stage,
            total=self.total,
            done=done,
            current=current,
            started_at=self._started,
        )

    def update_total(self, total: int) -> None:
        """Call after total is known (e.g. after loading products)."""
        self.total = total

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if exc_type is None:
                write_status(
                    self.automation,
                    status="completed",
                    stage="Done",
                    total=self.total,
                    done=self.total,
                    started_at=self._started,
                )
            else:
                write_status(
                    self.automation,
                    status="failed",
                    stage="Error",
                    total=self.total,
                    error=str(exc_val) if exc_val else "Unknown error",
                    started_at=self._started,
                )
        finally:
            # Always release lock — even if status write fails — so a crashed
            # status write can't permanently jam the next run.
            release_lock(self.automation)
        return False  # don't suppress exceptions
