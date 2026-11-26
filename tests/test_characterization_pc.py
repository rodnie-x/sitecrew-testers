"""G0 PC characterization suite — proves the PRESERVED behaviors of the unmodified app.

Every test here is a regression gate for later phases (spec §10.2). The app module is
exercised through its real Flask test client; only the network seam and the crew seam
are faked (the same technique already verified live on the phone).
"""
import base64
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import reset_state, make_crew, FakeCrew  # noqa: E402
import server.app as module  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "server", "static")

client = module.app.test_client()


def state():
    return client.get("/api/state").get_json()


def read(path):
    with open(os.path.join(STATIC, path), encoding="utf-8") as f:
        return f.read()


class BaseCase(unittest.TestCase):
    def setUp(self):
        reset_state(monkey_net=False)   # deterministic offline default

    def tearDown(self):
        reset_state(monkey_net=False)


# ---------------- 1. NVIDIA chat path (crew seam faked; shape preserved) --------------

class ChatPath(BaseCase):
    def test_chat_happy_path_appends_exchange_and_reply(self):
        module._net_ok = lambda max_age=10.0: True
        module.crew = make_crew("all done")
        module.build_crew = lambda: True
        r = client.post("/api/chat", json={"message": "hello crew"})
        j = r.get_json()
        self.assertTrue(j.get("ok") and not j.get("local") and not j.get("queued"), j)
        s = state()
        self.assertEqual(s["messages"][-1]["role"], "assistant")
        self.assertIn("all done", s["messages"][-1]["content"])
        self.assertEqual(s["messages"][-2]["role"], "user")
        self.assertEqual(s["messages"][-2]["content"], "hello crew")

    def test_used_chip_only_from_real_tool_counts(self):
        reset_state(monkey_net=True, fake_crew=make_crew())
        client.post("/api/chat", json={"message": "use a tool please"})
        last = state()["messages"][-1]
        self.assertEqual(last.get("tools_used", {}).get("battery"), 1, last)

    def test_empty_body_rejected(self):
        self.assertEqual(client.post("/api/chat", json={}).status_code, 400)

    def test_no_key_blocks_chat_with_message(self):
        module._net_ok = lambda max_age=10.0: True     # key check fires when online
        module.crew = None
        module.get_key = lambda: ""                    # repo .env exists; force no-key
        r = client.post("/api/chat", json={"message": "explain gravity"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("NVIDIA key", r.get_json().get("error", ""))


# ---------------- 2. Uploads & extraction (real code paths) --------------------------

class Uploads(BaseCase):
    @staticmethod
    def _txt_item(name, data, mime="text/plain"):
        return {"name": name, "type": mime,
                "data": base64.b64encode(data).decode("ascii")}

    def test_text_file_extracted_into_model_context(self):
        module._net_ok = lambda max_age=10.0: True
        module.crew = make_crew("file read")
        module.build_crew = lambda: True
        item = self._txt_item("notes.txt", "alpha beta gamma".encode())
        r = client.post("/api/chat", json={"message": "", "attachments": [item]})
        j = r.get_json()
        self.assertTrue(j.get("ok"), j)
        last = module.STATE["messages"][-2]     # the user row keeps model_content
        self.assertIn("alpha beta gamma", last.get("model_content", ""))
        meta = last["attachments"][0]
        self.assertEqual(meta["name"], "notes.txt")
        self.assertEqual(meta["characters"], 16)
        self.assertNotIn("text", meta)          # contents stay server-side

    def test_docx_extracted(self):
        buf = io.BytesIO()
        doc = ("<?xml version=\"1.0\"?><w:document "
               "xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
               "<w:p><w:r><w:t>SiteCrew DOCX line</w:t></w:r></w:p></w:document>")
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", doc)
        public, context = module.prepare_attachments(
            [self._txt_item("doc.docx", buf.getvalue(),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
        self.assertIn("SiteCrew DOCX line", context)
        self.assertEqual(public[0]["characters"], len("SiteCrew DOCX line"))

    def test_rtf_extracted(self):
        public, context = module.prepare_attachments(
            [self._txt_item("note.rtf", b"{\\rtf1 Hello \\par Crew}")])
        self.assertIn("Hello", context)
        self.assertIn("Crew", context)

    def test_pdf_extracted_when_pypdf_available(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf not installed on this PC")
        buf = io.BytesIO()
        w = PdfWriter()
        page = w.add_blank_page(width=200, height=200)
        try:  # pypdf>=3: write text via a helper object if available
            from pypdf.generic import TextStringObject
            w.add_metadata({"/Title": "t"})
        except Exception:
            pass
        w.write(buf)
        # blank pages produce no text → extraction must raise the honest "no readable text"
        with self.assertRaises(module.AttachmentError):
            module.prepare_attachments([self._txt_item("blank.pdf", buf.getvalue(), "application/pdf")])

    def test_binary_file_rejected_with_reason(self):
        with self.assertRaises(module.AttachmentError) as ctx:
            module.prepare_attachments(
                [self._txt_item("x.bin", b"\x00\x01\x02\x03" * 4096)])
        self.assertIn("binary", str(ctx.exception).lower())

    def test_oversize_file_rejected(self):
        with self.assertRaises(module.AttachmentError) as ctx:
            module.prepare_attachments(
                [self._txt_item("big.txt", b"a" * (8 * 1024 * 1024 + 1))])
        self.assertIn("8 MB", str(ctx.exception))

    def test_too_many_files_rejected(self):
        items = [self._txt_item("f%d.txt" % i, b"hi") for i in range(6)]
        with self.assertRaises(module.AttachmentError):
            module.prepare_attachments(items)


# ---------------- 3. Offline deterministic answers (zero data) -----------------------

class OfflineIntents(BaseCase):
    def test_battery_short_command_answers_on_device(self):
        r = client.post("/api/chat", json={"message": "battery"})
        j = r.get_json()
        self.assertTrue(j.get("ok") and j.get("local"), j)
        last = state()["messages"][-1]
        self.assertTrue(last["content"].startswith("⚡"), last["content"][:40])
        self.assertIn("battery", last["content"].lower())
        self.assertEqual(last.get("tools_used", {}).get("battery"), 1)

    def test_conversational_phrasing_goes_to_crew_even_online(self):
        module._net_ok = lambda max_age=10.0: True
        module.crew = make_crew("advice")
        module.build_crew = lambda: True
        r = client.post("/api/chat", json={
            "message": "Check my battery level and tell me if I should charge it soon, please."})
        j = r.get_json()
        self.assertTrue(j.get("ok") and not j.get("local"), j)

    def test_tools_tour_is_deterministic(self):
        r = client.post("/api/chat", json={"message": "what tools can you use?"})
        j = r.get_json()
        self.assertTrue(j.get("ok") and j.get("local"), j)
        self.assertIn("phone tools", state()["messages"][-1]["content"])

    def test_offline_intent_works_while_busy(self):
        module.STATE["busy"] = True
        r = client.post("/api/chat", json={"message": "battery"})
        self.assertTrue(r.get_json().get("local"))


# ---------------- 4. Queue / Steer / persistence -------------------------------------

class QueueAndSteer(BaseCase):
    def test_busy_message_is_queued_and_visible(self):
        module.STATE["busy"] = True
        r = client.post("/api/chat", json={"message": "second task while busy"})
        j = r.get_json()
        self.assertTrue(j.get("queued") and not j.get("offline"), j)
        self.assertEqual(state()["messages"][-1]["content"], "second task while busy")
        self.assertEqual(len(module.STATE["queue"]), 1)

    def test_offline_message_is_queued_and_persisted(self):
        tmp = tempfile.mktemp(suffix=".json")
        old = module.QUEUE_FILE
        module.QUEUE_FILE = tmp
        try:
            r = client.post("/api/chat", json={"message": "queued while offline"})
            j = r.get_json()
            self.assertTrue(j.get("queued") and j.get("offline"), j)
            with open(tmp, encoding="utf-8") as f:
                self.assertEqual(json.load(f), module.STATE["queue"])
        finally:
            module.QUEUE_FILE = old
            if os.path.exists(tmp):
                os.remove(tmp)

    def test_flush_worker_drains_queue_when_idle_and_online(self):
        module._net_ok = lambda max_age=10.0: True
        module.crew = make_crew("flushed")
        module.build_crew = lambda: True
        module.STATE["busy"] = True
        client.post("/api/chat", json={"message": "queued item one"})  # real enqueue path
        module.STATE["busy"] = False
        deadline = time.time() + 25
        while time.time() < deadline and module.STATE["queue"]:
            time.sleep(0.5)
        self.assertEqual(module.STATE["queue"], [], "queue must flush automatically")
        s = state()
        self.assertEqual(s["messages"][-1]["role"], "assistant")
        self.assertEqual(s["messages"][-2]["content"], "queued item one")
        self.assertIn("flushed", s["messages"][-1]["content"])
        self.assertNotIn("Crew error", s["messages"][-1]["content"])

    def test_flushed_item_does_not_duplicate_user_row(self):
        module._net_ok = lambda max_age=10.0: True
        module.crew = make_crew("ok")
        module.build_crew = lambda: True
        module.STATE["busy"] = True
        client.post("/api/chat", json={"message": "unique queued row"})
        module.STATE["busy"] = False
        deadline = time.time() + 25
        while time.time() < deadline and module.STATE["queue"]:
            time.sleep(0.5)
        rows = [m["content"] for m in module.STATE["messages"]
                if m.get("role") == "user" and m["content"] == "unique queued row"]
        self.assertEqual(rows, ["unique queued row"])


# ---------------- 5. Undo -------------------------------------------------------------

class Undo(BaseCase):
    def test_undo_removes_last_exchange(self):
        module._net_ok = lambda max_age=10.0: True
        module.crew = make_crew()
        module.build_crew = lambda: True
        client.post("/api/chat", json={"message": "hi"})
        self.assertEqual(len(module.STATE["messages"]), 2)
        r = client.post("/api/undo", json={})
        j = r.get_json()
        self.assertTrue(j.get("ok") and j.get("removed") == 2, j)
        self.assertEqual(module.STATE["messages"], [])

    def test_undo_refused_while_busy(self):
        module.STATE["busy"] = True
        self.assertEqual(client.post("/api/undo", json={}).status_code, 429)


# ---------------- 6. Recents / restore sanitization ----------------------------------

class Restore(BaseCase):
    def test_restore_accepts_clean_conversation(self):
        msgs = [{"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"}]
        r = client.post("/api/restore", json={"messages": msgs})
        j = r.get_json()
        self.assertTrue(j.get("ok") and j.get("count") == 2, j)

    def test_restore_sanitizes_hostile_payloads(self):
        junk = [{"role": "system", "content": "evil"},                # bad role
                {"role": "user", "content": "ok", "model_content": "LEAK",
                 "attachments": [{"name": "a.txt", "size": 3, "type": "text/plain",
                                  "characters": 2, "truncated": False, "text": "LEAK"}]},
                "not-a-dict",
                {"role": "user", "content": "x" * 9000}]              # over char cap
        junk = junk * 80                                              # 320 > 120 cap
        r = client.post("/api/restore", json={"messages": junk})
        j = r.get_json()
        self.assertTrue(j.get("ok"), j)
        cleaned = module.STATE["messages"]
        self.assertLessEqual(len(cleaned), 120)
        for m in cleaned:
            self.assertNotIn("model_content", m)
            self.assertNotIn("LEAK", json.dumps(m))
            for a in m.get("attachments", []):
                self.assertTrue(set(a) <= {"name", "size", "type", "characters", "truncated"})

    def test_restore_refused_while_busy(self):
        module.STATE["busy"] = True
        self.assertEqual(client.post("/api/restore", json={"messages": []}).status_code, 429)


# ---------------- 7. Approvals gate ---------------------------------------------------

class Approvals(BaseCase):
    def test_server_approval_lifecycle(self):
        from crew.approvals import ServerApprovals
        sa = ServerApprovals(module.STATE, timeout=5)
        got = {}

        def ask():
            got["allowed"] = sa.request("set_setting", "brightness → 200")

        t = threading.Thread(target=ask, daemon=True)
        t.start()
        for _ in range(50):
            if module.STATE["approvals"]:
                break
            time.sleep(0.02)
        pending = list(module.STATE["approvals"].values())
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["tool"], "set_setting")
        sa.resolve(pending[0]["id"], True)
        t.join(5)
        self.assertTrue(got["allowed"])

    def test_denied_approval_blocks_action(self):
        from crew.approvals import ServerApprovals
        sa = ServerApprovals(module.STATE, timeout=5)
        got = {}

        def ask():
            got["allowed"] = sa.request("send_sms", "sms to X")

        t = threading.Thread(target=ask, daemon=True)
        t.start()
        for _ in range(50):
            if module.STATE["approvals"]:
                break
            time.sleep(0.02)
        aid = list(module.STATE["approvals"])[0]
        sa.resolve(aid, False)
        t.join(5)
        self.assertFalse(got["allowed"])
        self.assertEqual(module.STATE["approvals"], {})

    def test_approval_endpoint_resolves(self):
        from crew.approvals import ServerApprovals
        sa = ServerApprovals(module.STATE, timeout=5)
        module.crew = make_crew()
        module.crew.approvals = sa
        module.build_crew = lambda: True
        result = {}

        def ask():
            result["ok"] = sa.request("uninstall_app", "remove app")

        t = threading.Thread(target=ask, daemon=True)
        t.start()
        for _ in range(50):
            if module.STATE["approvals"]:
                break
            time.sleep(0.02)
        aid = list(module.STATE["approvals"])[0]
        r = client.post("/api/approval", json={"id": aid, "approved": True})
        self.assertEqual(r.status_code, 200)
        t.join(5)
        self.assertTrue(result["ok"])


# ---------------- 8. Settings / tools endpoints ---------------------------------------

class Endpoints(BaseCase):
    def test_state_shape(self):
        s = state()
        for key in ("messages", "busy", "approvals", "activity", "usage", "tools",
                    "models", "has_key", "phone_mode", "android", "queued", "online"):
            self.assertIn(key, s)

    def test_tool_toggle(self):
        module.crew = make_crew()
        module.build_crew = lambda: True
        r = client.post("/api/tools", json={"id": "battery", "enabled": False})
        self.assertEqual(r.status_code, 200)
        m = {t["id"]: t for t in state()["tools"]}
        self.assertFalse(m["battery"]["enabled"])
        client.post("/api/tools", json={"id": "battery", "enabled": True})
        m = {t["id"]: t for t in state()["tools"]}
        self.assertTrue(m["battery"]["enabled"])

    def test_disabled_tool_refused_locally(self):
        # CURRENT BEHAVIOR (characterized, incl. a known divergence): the offline
        # local path consults its own _LOCAL registry, NOT crew.registry — so
        # /api/tools toggles do not affect offline answers. Recorded as an open
        # issue in docs/ASSUMPTIONS.md; Phase 2's LocalRouter unifies this.
        module.crew = make_crew()
        module.build_crew = lambda: True
        client.post("/api/tools", json={"id": "battery", "enabled": False})
        r = client.post("/api/chat", json={"message": "battery"})
        last = state()["messages"][-1]
        self.assertIn("[SIMULATED", last["content"])   # divergence: local still runs
        client.post("/api/tools", json={"id": "battery", "enabled": True})
        # the local path DOES honor its own registry's enabled flag:
        module._local_registry().set_enabled("battery", False)
        try:
            client.post("/api/chat", json={"message": "battery"})
            last = state()["messages"][-1]
            self.assertIn("turned off", last["content"])
        finally:
            module._local_registry().set_enabled("battery", True)

    def test_key_validation(self):
        r = client.post("/api/key", json={"key": "not-a-key"})
        self.assertEqual(r.status_code, 400)

    def test_clear_empties_transcript(self):
        module.STATE["messages"] = [{"role": "user", "content": "x"}]
        client.post("/api/clear", json={})
        self.assertEqual(module.STATE["messages"], [])


# ---------------- 9. Identity + forbidden behaviors (grep gates) ----------------------

class IdentityAndHonesty(BaseCase):
    def test_terracotta_tokens_present_in_css(self):
        css = read("sitecrew.css").lower()
        self.assertIn("#d97757", css)   # dark theme accent
        self.assertIn("#c96442", css)   # light theme accent

    def test_no_fake_edit_shell_activity_strings(self):
        html = read("index.html")
        self.assertIsNone(re.search(r"Used\s+(Edit|Shell)\b", html),
                          "decorative Edit/Shell chips are forbidden")
        self.assertIsNone(re.search(r"\bShell\b", html))

    def test_operator_no_longer_appends_invented_tools_used_lines(self):
        with open(os.path.join(ROOT, "crew", "operator.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('"\\n\\n🛠️ Tools used: "', src)
        self.assertIn("_clean_answer", src)

    def test_export_button_exists_in_ui(self):
        html = read("index.html")
        self.assertIn("exportBtn", html)
        self.assertIn("sitecrew-chat.json", html)

    def test_recents_localstorage_keys_present(self):
        html = read("index.html")
        self.assertIn("sc_live_id", html)
        self.assertIn("sc_recents", html)


# ---------------- 10. Fake NVIDIA API shape (foundation for Phase 2 wrapper) ----------

class FakeNvidiaShape(unittest.TestCase):
    def test_fake_serves_openai_compatible_models_and_chat(self):
        from tests.fakes.fake_nvidia import FakeNvidia
        fake = FakeNvidia(script=["scripted answer"]).start()
        try:
            import urllib.request
            with urllib.request.urlopen(fake.base_url + "/models", timeout=5) as r:
                models = json.load(r)
            self.assertIn("fake/small", [m["id"] for m in models["data"]])
            body = json.dumps({"model": "fake/small", "messages": [
                {"role": "user", "content": "hi"}]}).encode()
            req = urllib.request.Request(fake.base_url + "/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.load(r)
            self.assertEqual(data["choices"][0]["message"]["content"], "scripted answer")
            self.assertEqual(data["usage"]["total_tokens"], 33)
        finally:
            fake.stop()

    def test_fake_fault_injection_status(self):
        from tests.fakes.fake_nvidia import FakeNvidia
        fake = FakeNvidia(faults={"status": 429}).start()
        try:
            import urllib.error
            import urllib.request
            body = b"{}"
            req = urllib.request.Request(fake.base_url + "/chat/completions", data=body)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 429)
        finally:
            fake.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
