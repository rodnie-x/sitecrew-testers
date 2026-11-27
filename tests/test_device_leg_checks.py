"""Boss fix 1 (P1.2-D ruling): prove the device leg's named-check keys against
REAL output — a real EventStore, a real check_invariants() block, fed through
the leg's pure evaluate_checks(). Plain SQLite's integrity_check returns
["ok"]; OUR check_invariants maps a pass to [], and evaluate_checks keys on
that. A wrong key would fail all 30 cycles' worth of evidence at the end, so
this is pinned here, on PC, before any phone time.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store.events import EventStore   # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# import the leg module (imports adb_helpers, but touches no device at import)
_spec = importlib.util.spec_from_file_location(
    "device_kill_leg", os.path.join(REPO, "tests", "device_kill_leg.py"))
_leg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_leg)


class EvaluateChecksAgainstRealOutput(unittest.TestCase):
    def test_named_checks_all_true_on_a_real_green_store(self):
        tmp = tempfile.mkdtemp(prefix="sc_chk_", dir="D:/" if os.path.exists("D:/") else None)
        st = EventStore(os.path.join(tmp, "chk.db"))

        def chat_row(conn):
            conn.execute("INSERT OR IGNORE INTO chats(id, title, created_at,"
                         " updated_at) VALUES('c_chk','chk',1,1)")

        import json as _json
        for i in range(5):
            # message scenario: the writer inserts the messages row and
            # RETURNS its id, which becomes the event id (P1.2 oracle:
            # messages.id == events.id)
            def msw(conn, seq, _i=i):
                chat_row(conn)
                mid = "m_chk_%d" % _i
                conn.execute(
                    "INSERT INTO messages(id, chat_id, role, kind,"
                    " content_json, status, created_at) VALUES(?,?,?,?,?,?,1)",
                    (mid, "c_chk", "user", "user", _json.dumps({"i": _i}),
                     "final"))
                return mid

            st.append("c_chk", "message.created", {"i": i},
                      state_writer=msw, strict=True)
        # the tool path too (orphan coverage)
        st.append("c_chk", "tool.committed", {"tool_use_id": "tu1",
                                              "name": "fs.read", "verified": True},
                  turn_id="t1", agent="chk", site="pc",
                  state_writer=lambda conn, seq: (
                      chat_row(conn),
                      conn.execute("INSERT INTO turns(id, chat_id, state,"
                                   " started_at) VALUES('t1','c_chk','running',1)"),
                      conn.execute("INSERT INTO tool_calls(id, turn_id,"
                                   " tool_use_id, name, status, committed_seq)"
                                   " VALUES('tc1','t1','tu1','fs.read',"
                                   "'committed',?)", (seq,))),
                  strict=True)
        inv = st.check_invariants()
        # REAL output shape (boss's worry: SQLite returns ["ok"], we map to [])
        self.assertEqual(inv["integrity_check"], [])
        self.assertEqual(inv["foreign_key_check"], [])
        self.assertEqual(inv["message_links"], [])
        self.assertEqual(inv["tool_links"], [])
        self.assertEqual(inv["seq_contiguous"], [])
        self.assertTrue(inv["_pass"])
        # paste the real block into the report evidence
        with open(os.path.join(REPO, "tests", "evidence",
                               "device_checks_invariants_block.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"invariants_block": inv}, f, indent=2)

        checks = _leg.evaluate_checks(
            inv, invalid=0, final_head=6, initial_head=0, ack_lines_delta=6,
            unacked_sum=0,
            mod_checks={"sitecrew/store/db.py": True,
                        "sitecrew/store/events.py": True,
                        "sitecrew/api_v2.py": True},
            server_binds=True,
            apk_checks={"apk_hashes_recorded": True,
                        "apk_matches_build_artifact": True})
        self.assertTrue(all(checks.values()),
                        "named checks not all True on a green store: %s"
                        % json.dumps(checks, indent=2))

    def test_evaluate_checks_catches_failures(self):
        """The pure function must actually FAIL on bad input (no rubber stamp)."""
        bad_inv = {"_pass": False, "integrity_check": ["corrupt"],
                   "foreign_key_check": [("t", 1)], "message_links": [{"x": 1}],
                   "tool_links": [{"y": 2}], "seq_contiguous": [{"s": 1}]}
        checks = _leg.evaluate_checks(
            bad_inv, invalid=2, final_head=9, initial_head=0,
            ack_lines_delta=5, unacked_sum=1, mod_checks={"a.py": False},
            server_binds=False, apk_checks={"apk_hashes_recorded": False,
                                            "artifact_compared": True,
                                            "artifact_match": False})
        falsy = [k for k, v in checks.items() if not v]
        self.assertEqual(sorted(falsy), sorted([
            "all_cycles_valid", "invariants_pass", "integrity_check_ok",
            "foreign_key_check_empty", "message_orphans_zero",
            "tool_orphans_zero", "seq_contiguous",
            "identity_head_eq_acks_plus_unacked",
            "store_module_hashes_match", "server_binds_127_0_0_1",
            "apk_hashes_recorded", "apk_matches_build_artifact"]))

    def test_apk_tri_state(self):
        """Boss pre-run fix 1: the artifact match is TRI-STATE.
        - artifact absent -> 'not compared', EXCLUDED from pass
        - artifact present -> must match (mismatch fails pass)
        """
        green_inv = {"_pass": True, "integrity_check": [],
                     "foreign_key_check": [], "message_links": [],
                     "tool_links": [], "seq_contiguous": []}
        ok = {"apk_hashes_recorded": True, "artifact_compared": False,
              "artifact_match": None}
        absent = _leg.evaluate_checks(green_inv, 0, 10, 0, 10, 0,
                                      {"a.py": True}, True, ok)
        self.assertTrue(absent["apk_matches_build_artifact"])
        self.assertTrue(all(absent.values()))  # absence no longer fails pass

        present_match = dict(ok, artifact_compared=True, artifact_match=True)
        matched = _leg.evaluate_checks(green_inv, 0, 10, 0, 10, 0,
                                       {"a.py": True}, True, present_match)
        self.assertTrue(matched["apk_matches_build_artifact"])

        present_mismatch = dict(ok, artifact_compared=True, artifact_match=False)
        mismatched = _leg.evaluate_checks(green_inv, 0, 10, 0, 10, 0,
                                          {"a.py": True}, True,
                                          present_mismatch)
        self.assertFalse(mismatched["apk_matches_build_artifact"])
        self.assertFalse(all(mismatched.values()))

        # hashes must still be recorded regardless of the tri-state
        no_hashes = dict(present_match, apk_hashes_recorded=False)
        failing = _leg.evaluate_checks(green_inv, 0, 10, 0, 10, 0,
                                       {"a.py": True}, True, no_hashes)
        self.assertFalse(all(failing.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
