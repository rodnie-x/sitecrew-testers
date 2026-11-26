"""SQLite foundation: connect factory, migrations 001/002, ids, canonical JSON.

Boss rulings implemented here (docs/BOSS_PHASE1_ORDER.md):
- one connect() factory sets foreign_keys=ON, synchronous=NORMAL, busy_timeout on
  EVERY connection (WAL persists in the file; the other pragmas do not);
- `meta(key, value)` table for import markers and source sha256s;
- user_version 1 after migration 001, 2 after migration 002;
- new_id(prefix) = prefix + 16 random hex chars; UNIQUE INDEX on events(id);
- migration 002 tables created EMPTY, new columns NULL — no seeding;
- chat ids: new_id('c') only; reject 'office' and ids containing ':' or ',';
  legacy ids outside ^[A-Za-z0-9_-]{1,64}$ remap deterministically via meta.
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

MIN_SQLITE = (3, 35, 0)          # boss precondition: assert at startup


def sqlite_version_tuple():
    return tuple(int(x) for x in sqlite3.sqlite_version.split("."))


def assert_sqlite_min_version():
    """Fail fast if the LINKED SQLite is older than the store requires (boss ruling:
    lint against the SQLite Python actually links — the bundled one on Android)."""
    if sqlite_version_tuple() < MIN_SQLITE:
        raise RuntimeError(
            "SQLite >= %s required; linked sqlite3 reports %s"
            % (".".join(map(str, MIN_SQLITE)), sqlite3.sqlite_version))


# ---------------------------------------------------------------- ids and json

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FORBIDDEN_IDS = ("office",)


def new_id(prefix, _rng=None):
    """prefix + 16 random hex chars (boss ruling; 4 hex would collide ~300 events)."""
    if _rng is not None:                      # injectable for deterministic tests
        return prefix + "".join(_rng.choice("0123456789abcdef") for _ in range(16))
    import secrets
    return prefix + secrets.token_hex(8)


def dumps(obj):
    """The ONE canonical JSON serializer for the store (boss ruling: one dumps())."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def utc_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def valid_chat_id(chat_id):
    return (isinstance(chat_id, str) and _ID_RE.match(chat_id or "")
            and chat_id not in _FORBIDDEN_IDS and ":" not in chat_id and "," not in chat_id)


def remap_legacy_chat_id(legacy_id, conn, mapper=None):
    """Deterministically remap a legacy id outside the safe charset; store mapping."""
    if valid_chat_id(legacy_id):
        return legacy_id
    row = conn.execute("SELECT value FROM meta WHERE key=?",
                       ("chat_id_map:" + (legacy_id or ""),)).fetchone()
    if row:
        return row[0]
    new = new_id("c") if mapper is None else mapper(legacy_id)
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)",
                 ("chat_id_map:" + (legacy_id or ""), new))
    return new

# ---------------------------------------------------------------- migrations

MIGRATION_001 = """
CREATE TABLE meta(key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
CREATE TABLE chats(id TEXT PRIMARY KEY, title TEXT, pinned INT DEFAULT 0, model TEXT,
  mem_enabled INT DEFAULT 1, head_message_id TEXT, created_at INT, updated_at INT, deleted_at INT);
CREATE TABLE messages(id TEXT PRIMARY KEY, chat_id TEXT, parent_id TEXT, role TEXT,
  kind TEXT,
  content_json TEXT, status TEXT,
  client_msg_id TEXT, turn_id TEXT, created_at INT, UNIQUE(chat_id, client_msg_id));
CREATE TABLE turns(id TEXT PRIMARY KEY, chat_id TEXT, user_message_id TEXT, state TEXT,
  step INT DEFAULT 0, attempt INT DEFAULT 0, source TEXT DEFAULT 'model',
  case_id TEXT,
  terminal_reason TEXT, error_code TEXT, started_at INT, ended_at INT);
CREATE TABLE events(chat_id TEXT, seq INTEGER, id TEXT, turn_id TEXT, type TEXT,
  data_json TEXT, ts INT, case_id TEXT, task_id TEXT, run_id TEXT, agent TEXT, site TEXT,
  PRIMARY KEY(chat_id, seq));
CREATE UNIQUE INDEX idx_events_id ON events(id);
CREATE TABLE tool_calls(id TEXT PRIMARY KEY, turn_id TEXT, tool_use_id TEXT, name TEXT,
  args_json TEXT, args_hash TEXT, risk TEXT,
  status TEXT,
  result_json TEXT, verify_json TEXT, error_code TEXT, attempt INT DEFAULT 0,
  started_at INT, ended_at INT, committed_seq INT,
  case_id TEXT, task_id TEXT, run_id TEXT, agent TEXT, site TEXT,
  paths_json TEXT, tier TEXT, grant_id TEXT);
CREATE INDEX idx_tool_calls_committed ON tool_calls(committed_seq);
CREATE TABLE approvals(id TEXT PRIMARY KEY, tool_call_id TEXT, args_hash TEXT,
  status TEXT,
  expires_at INT, decided_at INT);
CREATE TABLE queue(id TEXT PRIMARY KEY, chat_id TEXT, message_id TEXT,
  state TEXT,
  position INT);
CREATE TABLE undo_ops(id TEXT PRIMARY KEY, tool_call_id TEXT, plan_json TEXT,
  status TEXT,
  applied_at INT);
CREATE TABLE summaries(chat_id TEXT, version INT, covers_to_message_id TEXT, text TEXT,
  created_at INT, PRIMARY KEY(chat_id, version));
CREATE TABLE usage(day TEXT PRIMARY KEY, requests INT, tokens_in INT, tokens_out INT);
"""

MIGRATION_002 = """
CREATE TABLE agents(id TEXT PRIMARY KEY, title TEXT, room TEXT, reports_to TEXT,
  kind TEXT,
  tier TEXT, tools_json TEXT, sites_json TEXT, prompt_ref TEXT,
  max_steps INT, budget_calls INT, enabled INT DEFAULT 1, config_version INT);
CREATE TABLE cases(id TEXT PRIMARY KEY, chat_id TEXT, turn_id TEXT, title TEXT,
  kind TEXT,
  state TEXT,
  lead TEXT, budget_calls INT, calls_used INT DEFAULT 0,
  privacy TEXT DEFAULT 'normal',
  summary TEXT, opened_at INT, closed_at INT);
CREATE TABLE tasks(id TEXT PRIMARY KEY, case_id TEXT, parent_id TEXT, title TEXT,
  assignee TEXT, site TEXT,
  state TEXT,
  blocked_reason TEXT,
  depends_json TEXT, spec_json TEXT,
  attempt INT DEFAULT 0, claimed_by TEXT, lease_until INT,
  handoff_entry_id TEXT, created_at INT, updated_at INT);
CREATE TABLE agent_runs(id TEXT PRIMARY KEY, task_id TEXT, agent TEXT,
  state TEXT,
  model TEXT, step INT, heartbeat_at INT, started_at INT, ended_at INT,
  calls INT DEFAULT 0, tokens_in INT DEFAULT 0, tokens_out INT DEFAULT 0);
CREATE TABLE sites(id TEXT PRIMARY KEY, kind TEXT,
  name TEXT, state TEXT, last_seen INT, meta_json TEXT);
CREATE TABLE hq_links(site_id TEXT PRIMARY KEY, host TEXT, port INT, cert_sha256 TEXT,
  link_key_ref TEXT,
  paired_at INT, rotated_at INT, policy_digest TEXT);
CREATE TABLE workspaces(id TEXT PRIMARY KEY, site_id TEXT, path TEXT, label TEXT,
  mode TEXT,
  origin TEXT, created_at INT);
CREATE TABLE grants(id TEXT PRIMARY KEY, case_id TEXT, family TEXT, site_id TEXT, root TEXT,
  status TEXT, opened_at INT, expires_at INT, revoked_at INT);
CREATE TABLE path_locks(site_id TEXT, path TEXT, task_id TEXT, acquired_at INT,
  PRIMARY KEY(site_id, path));
CREATE TABLE file_snapshots(id TEXT PRIMARY KEY, tool_call_id TEXT, site_id TEXT, path TEXT,
  before_sha256 TEXT, after_sha256 TEXT, before_ref TEXT, size INT, created_at INT, expires_at INT);
CREATE TABLE brain_entries(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  tags_json TEXT DEFAULT '[]',
  author TEXT NOT NULL,
  case_id TEXT, task_id TEXT, run_id TEXT, site TEXT,
  subject_key TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  trust TEXT NOT NULL DEFAULT 'derived',
  visibility TEXT NOT NULL DEFAULT 'office',
  pinned INT DEFAULT 0, version INT NOT NULL DEFAULT 1, supersedes_id TEXT,
  evidence_json TEXT DEFAULT '[]',
  observed_at INT, ttl_s INT,
  created_at INT NOT NULL, updated_at INT NOT NULL, event_seq INT);
CREATE TABLE brain_links(from_id TEXT, to_id TEXT,
  rel TEXT,
  author TEXT, created_at INT, PRIMARY KEY(from_id, to_id, rel));
CREATE TABLE brain_revisions(entry_id TEXT, version INT, title TEXT, body TEXT,
  editor TEXT, ts INT, PRIMARY KEY(entry_id, version));
CREATE TABLE brain_terms(term TEXT, entry_id TEXT);
CREATE INDEX idx_brain_terms_entry ON brain_terms(entry_id);
"""

MIGRATIONS = {1: MIGRATION_001, 2: MIGRATION_002}

# ---------------------------------------------------------------- connect factory

_conn_lock = threading.Lock()


def connect(path, timeout=5.0, read_only=False):
    """The ONE connection factory (boss ruling): identical pragmas on every connection.

    read_only=True adds PRAGMA query_only=ON (boss precondition for read connections).
    check_same_thread=False: Flask threads and parked poll threads share connections;
    correctness comes from the writer lock (writes) and short reads, not thread affinity.
    """
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if read_only:
        conn.execute("PRAGMA query_only=ON")
    return conn


def migrate(path, failpoint=None):
    """Apply pending migrations atomically (boss rerun spec item 2 + ruling items 5/6).

    Each migration runs inside ONE BEGIN IMMEDIATE ... COMMIT with the PRAGMA
    user_version bump AND the user_version read inside the same transaction. A kill
    at any point rolls back the whole migration: the DB is always fully at the old
    version or fully at the new one.
    failpoint: optional callable(stage) invoked after EVERY statement and before
    user_version — stages: "stmt:<n>", "before_version" (deterministic kill test).
    Statements come from an explicit list (no split(';') parsing).
    """
    conn = connect(path)
    with _conn_lock:
        conn.execute("PRAGMA journal_mode=WAL")
        for target in sorted(MIGRATIONS):
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current >= target:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                statements = [s.strip() for s in _statements(MIGRATIONS[target])]
                for n, stmt in enumerate(statements):
                    conn.execute(stmt)
                    if failpoint is not None:
                        failpoint("stmt:%d" % n)
                if failpoint is not None:
                    failpoint("before_version")
                conn.execute(f"PRAGMA user_version={target}")
                inside = conn.execute("PRAGMA user_version").fetchone()[0]
                if inside != target:
                    raise RuntimeError("user_version did not stick")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            current = target
    return conn, target


def _statements(script):
    """Split a migration script into statements without regex guessing
    (boss ruling: replace split(';') with an explicit list or complete_statement)."""
    out = []
    buf = []
    for line in script.splitlines():
        buf.append(line)
        joined = "\n".join(buf)
        if sqlite3.complete_statement(joined):
            stmt = joined.strip()
            if stmt and not stmt.startswith("--"):
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def open_db(path):
    """connect + migrate in one call (the store's only entry point for a path)."""
    assert_sqlite_min_version()
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn, version = migrate(path)
    return conn, version


# ------------------------------------------------- cross-process writer lock
class ProcLock:
    """OS-level advisory lock on <path>.lock — flock (POSIX) or msvcrt (Windows).

    Boss ruling: use flock for the writer lock, NOT a lockfile's existence, so a
    SIGKILL cannot leave a stale lock (the OS releases the lock on process death).
    A second writer attempting to acquire gets DB_LOCKED.
    """

    def __init__(self, path):
        self.lock_path = path + ".lock"
        self._fd = None
        self._locked = False

    def acquire(self, blocking=True):
        self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            import fcntl                       # POSIX
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(self._fd, flags)
            self._locked = True
            return True
        except ImportError:
            import msvcrt                      # Windows
            os.lseek(self._fd, 0, os.SEEK_SET)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(self._fd, mode, 1)
                self._locked = True
                return True
            except OSError:
                os.close(self._fd)
                self._fd = None
                raise

    def release(self):
        if not self._locked:
            return
        try:
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except ImportError:
            import msvcrt
            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        os.close(self._fd)
        self._fd = None
        self._locked = False


DB_LOCKED = "DB_LOCKED"

# ---------------------------------------------------------------- EVENT_TYPES

V1_FAMILIES = ("turn", "message", "model", "tool", "gate", "approval", "undo", "system")
OFFICE_TYPES = (
    "case.opened", "case.routed", "plan.created", "plan.approved", "task.created",
    "task.claimed", "task.started", "task.blocked", "task.unblocked", "task.completed",
    "task.failed", "task.skipped", "case.closed", "agent.run.started", "agent.run.state",
    "agent.run.ended", "office.paused", "office.resumed", "ratelimit.entered",
    "ratelimit.cleared")
BRAIN_TYPES = (
    "brain.briefing.built", "brain.entry.written", "brain.entry.updated",
    "brain.entry.superseded", "brain.entry.retracted", "brain.proposal.created",
    "brain.proposal.resolved", "brain.conflict.flagged", "brain.conflict.resolved",
    "brain.entry.suspect")
SITE_TYPES = (
    "hq.pairing.started", "hq.paired", "hq.state.changed", "hq.policy.changed",
    "hq.unpaired", "grant.opened", "grant.revoked", "fs.snapshot.created",
    "fs.trash.moved", "fs.trash.restored")

EVENT_TYPES = set(OFFICE_TYPES) | set(BRAIN_TYPES) | set(SITE_TYPES)
# Import events (boss ruling): system.import.chat + system.import.completed.
EVENT_TYPES.add("system.import.chat")
EVENT_TYPES.add("system.import.completed")


def event_type_known(type_name):
    """Exact new-family name, or any v1.0 family prefix (boss ruling)."""
    if type_name in EVENT_TYPES:
        return True
    return any(type_name.startswith(fam + ".") for fam in V1_FAMILIES)
