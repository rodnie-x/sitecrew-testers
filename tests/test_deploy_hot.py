"""Deploy-helper fail-closed tests. No adb and no physical device is touched."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from scripts import deploy_hot  # noqa: E402


def good_hash():
    return "a" * 64


def bad_hash():
    return "b" * 64


class DeployHotFailClosed(unittest.TestCase):
    def test_dry_run_missing_or_outside_fails_closed(self):
        outside = tempfile.mkdtemp(prefix="sc_deploy_outside_")
        missing = os.path.join("no_such", "file.py")
        for rel in (missing, os.path.relpath(outside, ROOT)):
            with mock.patch.object(deploy_hot, "resolve_local",
                                   return_value=(None, "forced error")):
                report = {"files": [], "ok": True}
                prepared = deploy_hot.perform_deploy(
                    report, type("Args", (), {"backup": False, "paths": [rel]})(),
                    "SERIAL")
            self.assertFalse(prepared)
            self.assertFalse(report["ok"])
            self.assertFalse(report["files"][0]["ok"])

    def test_backup_failure_blocks_overwrite(self):
        with mock.patch.object(deploy_hot, "remote_hash",
                               return_value={"ok": False, "exists": False,
                                             "error": "no backup"}), \
             mock.patch.object(deploy_hot, "shell_run_as",
                               return_value={"rc": 1, "stdout": "", "stderr": "no"}):
            result = deploy_hot.backup_remote("SERIAL", "server/app.py", good_hash())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "copy-failed")

    def test_existing_backup_mismatch_blocks_deploy(self):
        with mock.patch.object(deploy_hot, "remote_hash",
                               return_value={"ok": True, "sha256": bad_hash()}):
            result = deploy_hot.backup_remote("SERIAL", "server/app.py", good_hash())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "existing-backup-mismatch")

    def test_mid_list_failure_does_not_report_pass(self):
        report = {"files": [], "ok": True}
        prepared = [
            {"local": "a.py", "local_abs": os.path.join(ROOT, "a.py"),
             "sha256": good_hash(), "bytes": 1},
            {"local": "b.py", "local_abs": os.path.join(ROOT, "b.py"),
             "sha256": bad_hash(), "bytes": 1},
        ]
        with mock.patch.object(deploy_hot, "deploy_one", side_effect=[
                {"ok": True, "local": "a.py", "remote": "a.py",
                 "sha256": good_hash(), "rm": {"rc": 0}},
                {"ok": False, "local": "b.py", "error": "hash mismatch"}]):
            ok = deploy_hot.perform_deploy(
                report, type("Args", (), {"backup": False, "paths": []})(),
                "SERIAL", prepared_override=prepared)
        self.assertFalse(ok)
        self.assertFalse(report["ok"])
        self.assertNotIn("deployed", report)

    def test_restart_stale_process_fails(self):
        report = {}
        with mock.patch.object(deploy_hot, "forward",
                               return_value={"rc": 0}), \
             mock.patch.object(deploy_hot, "pids",
                               side_effect=[["111"], ["111"]]), \
             mock.patch.object(deploy_hot, "run_adb", side_effect=[
                 {"rc": 1, "stdout": "", "stderr": "force-stop denied"},
                 {"rc": 0, "stdout": "Starting: Intent", "stderr": ""},
             ]), \
             mock.patch.object(deploy_hot, "wait_server", return_value=True):
            ok = deploy_hot.restart_and_verify("SERIAL", report, "http://x")
        self.assertFalse(ok)
        self.assertFalse(report["restart"]["ok"])
        self.assertEqual(report["restart"]["force_stop"]["rc"], 1)

    def test_restart_requires_new_pid(self):
        report = {}
        with mock.patch.object(deploy_hot, "forward",
                               return_value={"rc": 0}), \
             mock.patch.object(deploy_hot, "pids",
                               side_effect=[["111"], ["111"]]), \
             mock.patch.object(deploy_hot, "run_adb", side_effect=[
                 {"rc": 0, "stdout": "", "stderr": ""},
                 {"rc": 0, "stdout": "Starting: Intent", "stderr": ""},
             ]), \
             mock.patch.object(deploy_hot, "wait_server", return_value=True):
            ok = deploy_hot.restart_and_verify("SERIAL", report, "http://x")
        self.assertFalse(ok)
        self.assertFalse(report["restart"]["pids_changed"])

    def test_malformed_remote_hash_is_rejected(self):
        with mock.patch.object(deploy_hot, "shell_run_as",
                               return_value={"rc": 0, "stdout": "notahash x\n",
                                             "stderr": ""}):
            result = deploy_hot.remote_hash("SERIAL", "x.py")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "malformed sha256 output")


if __name__ == "__main__":
    unittest.main(verbosity=2)
