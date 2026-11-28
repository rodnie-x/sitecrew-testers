"""SQLite lint + step-3 preconditions (boss rulings):

- device SQLite lint: the store's SQL must not use features newer than the phone's
  SQLite 3.35.5 (unixepoch(, ->>, ->, STRICT, concat(, string_agg(, octet_length()
  — banned outright; json_* banned UNLESS JSON1 is proven in the linked runtime).
- startup version assert: sqlite3.sqlite_version >= 3.35.0.
- query_only read connections refuse writes.
- golden sqlite_master dump survives a reopen byte-equal.
- ProcLock: a SIGKILLed holder cannot leave a stale lock (OS releases it).
"""
import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANNED = ["unixepoch(", "->>", "->", "STRICT", "concat(", "string_agg(",
          "octet_length("]


def string_constants_of(path):
    """String constants in a module via AST, EXCLUDING docstrings (boss ruling:
    the lint inspects SQL strings — prose mentioning STRICT etc. is not SQL)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            out.append(node.value)
    return out


def json1_available():
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("SELECT json_extract('{}','$.a')")
        conn.close()
        return True
    except Exception:
        return False


class SQLiteLint(unittest.TestCase):
    def test_no_banned_features_in_store_sql(self):
        """Only strings that ARE SQL (contain a SQL keyword) are linted — docstrings
        mentioning STRICT etc. are not SQL."""
        store_dir = os.path.join(REPO, "sitecrew", "store")
        sql_markers = ("SELECT", "INSERT", "CREATE", "PRAGMA", "BEGIN", "UPDATE",
                       "DELETE", "COMMIT", "ROLLBACK")
        offenders = []
        for fn in ("db.py", "events.py", "search.py"):
            path = os.path.join(store_dir, fn)
            for const in string_constants_of(path):
                if not any(m in const.upper() for m in sql_markers):
                    continue                                   # not SQL
                up = const.upper()
                for token in BANNED:
                    if token.upper() in up:
                        offenders.append((fn, token, const[:60]))
        self.assertEqual(offenders, [], "banned SQLite features: %s" % offenders)

    def test_json_gated_on_runtime_json1(self):
        """If the store's SQL uses json_*, JSON1 must work in the linked runtime."""
        uses_json = False
        for fn in ("db.py", "events.py", "search.py"):
            path = os.path.join(store_dir_path(fn), )
            for const in string_constants_of(path):
                if "json_" in const.lower():
                    uses_json = True
        if uses_json:
            self.assertTrue(json1_available(),
                            "store SQL uses json_* but JSON1 is unavailable")
        # recorded for the device evidence trail
        print("json1_available:", json1_available(), "uses_json:", uses_json)


def store_dir_path(fn):
    return os.path.join(REPO, "sitecrew", "store", fn)


class VersionAssert(unittest.TestCase):
    def test_startup_assert(self):
        # current runtime passes (PC 3.49.1, device 3.35.5 — both >= 3.35.0)
        sdb.assert_sqlite_min_version()

    def test_startup_assert_fails_on_old(self):
        old = sdb.MIN_SQLITE
        sdb.MIN_SQLITE = (99, 0, 0)
        try:
            with self.assertRaises(RuntimeError):
                sdb.assert_sqlite_min_version()
        finally:
            sdb.MIN_SQLITE = old


class ReadOnlyConnections(unittest.TestCase):
    def test_query_only_refuses_writes(self):
        tmp = tempfile.mkdtemp(prefix="sc_ro_")
        path = os.path.join(tmp, "sitecrew.db")
        conn, _ = sdb.open_db(path)
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        conn.close()
        ro = sdb.connect(path, read_only=True)
        self.assertEqual(ro.execute("PRAGMA query_only").fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO t(x) VALUES(1)")
        ro.close()


class GoldenSchema(unittest.TestCase):
    def test_sqlite_master_dump_survives_reopen(self):
        tmp = tempfile.mkdtemp(prefix="sc_gold_")
        path = os.path.join(tmp, "sitecrew.db")
        conn1, _ = sdb.open_db(path)
        golden = sorted((r["name"], r["type"], r["sql"] or "")
                        for r in conn1.execute(
                            "SELECT name, type, sql FROM sqlite_master"))
        conn1.close()
        conn2, _ = sdb.open_db(path)
        again = sorted((r["name"], r["type"], r["sql"] or "")
                       for r in conn2.execute(
                           "SELECT name, type, sql FROM sqlite_master"))
        conn2.close()
        self.assertEqual(golden, again)


class ProcLockTest(unittest.TestCase):
    def test_second_writer_gets_db_locked(self):
        tmp = tempfile.mkdtemp(prefix="sc_lock_")
        path = os.path.join(tmp, "sitecrew.db")
        lock1 = sdb.ProcLock(path)
        lock1.acquire()
        lock2 = sdb.ProcLock(path)
        with self.assertRaises(OSError):
            lock2.acquire(blocking=False)
        lock1.release()
        # after release, acquire works again
        lock2.acquire(blocking=False)
        lock2.release()

    def test_sigkill_cannot_leave_stale_lock(self):
        """Boss ruling: flock (not lockfile existence) — a killed holder releases."""
        tmp = tempfile.mkdtemp(prefix="sc_lock2_")
        path = os.path.join(tmp, "sitecrew.db")
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, %r);"
             "from sitecrew.store import db as sdb;"
             "lk = sdb.ProcLock(%r); lk.acquire();"
             "print('HELD', flush=True); time.sleep(300)" % (REPO, path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            line = holder.stdout.readline().decode().strip()
            self.assertEqual(line, "HELD")
            lock_after = sdb.ProcLock(path)
            holder.kill()                     # SIGKILL-equivalent hard kill
            holder.wait(timeout=30)
            # the OS must have dropped the lock: non-blocking acquire succeeds
            lock_after.acquire(blocking=False)
            lock_after.release()
        finally:
            holder.kill()
            holder.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
