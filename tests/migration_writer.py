"""Migration subprocess for the migration kill test: runs open_db (0 → 1 → 2)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb  # noqa: E402

if __name__ == "__main__":
    conn, version = sdb.open_db(sys.argv[1])
    conn.close()
