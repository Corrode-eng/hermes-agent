"""Regression: done-state watchdog + the t_stormtest drift scenario.

Background (audit task t_a7d50ae9 / t_8b78a9a3): tasks were observed in
status 'done' with NO completed run on record, and decomposed "phantom
parents" existed only as orphan task_events rows (no tasks row) — e.g.
t_52d16fca / 't_stormtest' itself never existed as a row anywhere. The
drift signature is a task row with status='done' and no task_runs row
with status='done'/outcome='completed'. It was produced by paths that
bypass the kernel (test fixtures writing real tasks straight into the
production DB) and by bare CLI empty handoffs (complete without
result/summary/metadata on a never-claimed task).

These tests exercise the *on-demand* watchdog mode (`--check`) against an
isolated board DB (kanban_home fixture — the hermetic pattern used
throughout tests/hermes_cli: HERMES_KANBAN_* is cleared by the autouse
``_hermetic_environment`` fixture in tests/conftest.py, so nothing
resolves to the production board). Defined locally so this file is
self-contained on any base branch.

Invariant under test (watchdog acceptance):
  * the watchdog script exits NON-ZERO when any task is 'done' without a
    completed run on record;
  * valid done tasks (with completed runs) and phantom references (rows in
    task_links with no tasks row) produce NO false positives;
  * a run marked terminal-complete with a non-completed outcome (e.g. a
    crashed run mislabeled done) is also flagged as drift.

Kernel-side enforcement of this invariant (rejecting/repairing a
done-without-run transition at the ``complete_task`` boundary) is tracked
separately (t_ce9b3f9f / t_5aa3b2e5) and is NOT assumed here — these tests
only pin the watchdog's own detection behavior against directly-written
drift, which is exercisable on main today.
"""

import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

# Path to the watchdog script. Defaults to the homelab install location;
# override with KANBAN_WATCHDOG_SCRIPT for other environments.
WATCHDOG = Path(
    os.environ.get("KANBAN_WATCHDOG_SCRIPT", "/root/.hermes/scripts/kanban-done-watchdog.sh")
)

pytestmark = pytest.mark.skipif(
    not WATCHDOG.exists(),
    reason=f"kanban-done-watchdog.sh not installed (set KANBAN_WATCHDOG_SCRIPT): {WATCHDOG}",
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB.

    Hermetic by design: HERMES_KANBAN_DB is left unset (the autouse
    ``_hermetic_environment`` fixture in tests/conftest.py clears all
    ``HERMES_KANBAN_*`` vars), so neither ``connect()`` nor the watchdog
    can resolve to the production board. Defined locally so this file is
    self-contained on any base branch.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _board_db(kanban_home) -> Path:
    """The isolated board DB created by the kanban_home fixture."""
    return Path(os.environ["HERMES_HOME"]) / "kanban.db"


def _watchdog_check(db: Path) -> subprocess.CompletedProcess:
    """Run the watchdog in on-demand --check mode against ``db``."""
    return subprocess.run(
        ["bash", str(WATCHDOG), "--check", "--db", str(db)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _completed_run_count(db: Path, task_id: str) -> int:
    """Count runs matching the watchdog's own definition of "completed".

    The watchdog treats status IN ('done', 'completed') with
    outcome='completed' as valid — historical rows use status='completed',
    current kernel code may use status='done'. Both are non-drift.
    """
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM task_runs "
            "WHERE task_id=? AND status IN ('done', 'completed') AND outcome='completed'",
            (task_id,),
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Watchdog detection — reproduces the t_stormtest drift
# ---------------------------------------------------------------------------


def test_watchdog_exits_nonzero_on_done_without_completed_run(kanban_home):
    """A task marked done with no completed run is flagged, exit != 0.

    Reproduces the t_stormtest drift scenario using the pollution path the
    audit identified: a row written directly to the board DB, bypassing the
    kernel's state-transition logic (status='done', zero runs).
    """
    db = _board_db(kanban_home)
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO tasks"
                " (id, title, assignee, status, priority, created_at, workspace_kind)"
                " VALUES (?, ?, ?, 'done', 0, ?, 'scratch')",
                ("t_stormtest_drift", "storm test drift", "worker", int(time.time())),
            )
    finally:
        conn.close()

    proc = _watchdog_check(db)
    assert proc.returncode != 0, (
        "watchdog must exit non-zero when a done task has no completed run\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "t_stormtest_drift" in proc.stdout
    assert _completed_run_count(db, "t_stormtest_drift") == 0


def test_watchdog_exits_nonzero_on_done_run_with_bad_outcome(kanban_home):
    """A run marked done with a non-completed outcome is also drift."""
    db = _board_db(kanban_home)
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="bad done run", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                (int(time.time()), tid),
            )
            conn.execute(
                "INSERT INTO task_runs"
                " (task_id, profile, status, outcome, started_at, ended_at)"
                " VALUES (?, 'worker', 'done', 'crashed', ?, ?)",
                (tid, int(time.time()) - 10, int(time.time())),
            )
    finally:
        conn.close()

    proc = _watchdog_check(db)
    assert proc.returncode != 0
    assert tid in proc.stdout


# ---------------------------------------------------------------------------
# No false positives
# ---------------------------------------------------------------------------


def test_watchdog_exits_zero_on_valid_done_task(kanban_home):
    """A legitimately done task (completed run on record) is NOT flagged."""
    db = _board_db(kanban_home)
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="valid done", assignee="worker")
        ok = kb.complete_task(conn, tid, result="genuinely complete")
        assert ok
    finally:
        conn.close()

    assert _completed_run_count(db, tid) == 1
    proc = _watchdog_check(db)
    assert proc.returncode == 0, f"false positive on valid done task\nstdout: {proc.stdout}"


def test_watchdog_ignores_phantom_references(kanban_home):
    """Orphan links to tasks with no row must not be flagged.

    Audit evidence: phantom parents (e.g. t_52d16fca) existed only as
    orphan task_events/task_links rows with no tasks row. The watchdog
    scans tasks.status only, so those references are not drift.
    """
    db = _board_db(kanban_home)
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="valid done", assignee="worker")
        ok = kb.complete_task(conn, tid, result="genuinely complete")
        assert ok
        with kb.write_txn(conn):
            # t_stormtest itself: the phantom that only ever appeared in
            # prose and in orphan rows — never a real tasks row.
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                ("t_stormtest", tid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                ("t_52d16fca", tid),
            )
    finally:
        conn.close()

    proc = _watchdog_check(db)
    assert proc.returncode == 0, f"phantom references must not be drift\nstdout: {proc.stdout}"


def test_watchdog_reports_multiple_drift_tasks(kanban_home):
    """Two independent drift rows are both surfaced in one check."""
    db = _board_db(kanban_home)
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            for tid in ("t_drift_alpha", "t_drift_beta"):
                conn.execute(
                    "INSERT INTO tasks"
                    " (id, title, assignee, status, priority, created_at, workspace_kind)"
                    " VALUES (?, ?, ?, 'done', 0, ?, 'scratch')",
                    (tid, f"{tid} drift", "worker", int(time.time())),
                )
    finally:
        conn.close()

    proc = _watchdog_check(db)
    assert proc.returncode != 0
    assert "t_drift_alpha" in proc.stdout
    assert "t_drift_beta" in proc.stdout
