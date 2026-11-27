"""Boss P1.2-D ruling: the device SQLite floor is 3.35.5 (TECNO LH7n, Android 14).

The store code must therefore never use SQLite features newer than 3.35.5:
  - no STRICT tables (3.37)
  - no -> or ->> JSON operators (3.38)
  - no unixepoch() (3.38)
Static lint over the store modules + the server (the SQL-writing surfaces),
plus a runtime floor assertion for the PC test host. The device's
store-state facts carry a live JSON-function probe (see server/app.py).
"""
import glob
import os
import re
import sqlite3
import sys
import unittest

REPO = __file__.rsplit("tests", 1)[0]

# the SQL-writing surfaces (boss: "back it with a PC test or lint")
TARGETS = [
    "sitecrew/store/db.py",
    "sitecrew/store/events.py",
    "sitecrew/store/search.py",
    "server/app.py",
]

FORBIDDEN = [
    # (regex, feature, first sqlite version)
    (r"\bSTRICT\b", "STRICT tables", "3.37"),
    (r"->>", "JSON ->> operator", "3.38"),
    (r"->\s*'", "JSON -> operator", "3.38"),
    (r'->\s*"', "JSON -> operator", "3.38"),
    (r"\bunixepoch\s*\(", "unixepoch()", "3.38"),
]


class DeviceSQLiteFloor(unittest.TestCase):
    def test_no_post_3_35_5_sql_features(self):
        violations = []
        for rel in TARGETS:
            p = os.path.join(REPO, rel.replace("/", os.sep))
            if not os.path.exists(p):
                continue
            src = open(p, encoding="utf-8").read()
            for line_no, line in enumerate(src.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""') \
                        or stripped.startswith("'''"):
                    continue                     # comments/docstring lines
                for pattern, feature, ver in FORBIDDEN:
                    if re.search(pattern, line):
                        violations.append("%s:%d uses %s (needs SQLite %s)"
                                          % (rel, line_no, feature, ver))
        self.assertEqual(violations, [],
                         "device SQLite floor 3.35.5 violated:\n"
                         + "\n".join(violations))

    def test_pc_runtime_meets_floor_and_json_functions(self):
        # the floor is a MINIMUM: the device runs 3.35.5, PC must be >= that
        # for the PC halves to be a fair proxy
        self.assertGreaterEqual(tuple(int(x) for x in
                                      sqlite3.sqlite_version.split(".")[:3]),
                                (3, 35, 5))
        conn = sqlite3.connect(":memory:")
        # the store's SQL uses json_extract (3.9+) — pin it works
        v = conn.execute(
            "SELECT json_extract('{\"a\": {\"b\": 7}}', '$.a.b')").fetchone()[0]
        self.assertEqual(v, 7)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
