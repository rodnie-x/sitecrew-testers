"""D7 split (boss ruling): the failing G0 device test's PC half.

Proves the full legacy chat path (express lane → laborer → client → reply) against
the FAKE NVIDIA server — no real internet, no real key. The device half lives in
tests/test_characterization_device.py with a connectivity preflight (offline →
NOT RUN, never FAIL).
"""
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CREW_PRESENT = importlib.util.find_spec("crew") is not None
if CREW_PRESENT:
    from tests.fakes.fake_nvidia import FakeNvidia   # noqa: E402
else:
    FakeNvidia = None


@unittest.skipUnless(CREW_PRESENT, "full product modules are not in the public subset")
class ChatAgainstFakeNvidia(unittest.TestCase):
    def setUp(self):
        """Point NvidiaClient at the fake server for the duration of each test.

        NVIDIA_BASE_URL is bound as a default arg at definition time, so patching
        the module constant is not enough — patch the constructor's base_url.
        """
        import crew.nvidia as nvidia_mod
        self._nvidia = nvidia_mod
        self._fake = FakeNvidia(script=[
            "No need to charge soon — battery is at 100% and charging."]).start()
        self._orig_init = nvidia_mod.NvidiaClient.__init__

        def patched_init(client_self, api_key=None, base_url=None, usage=None,
                         _fake_url=self._fake.base_url, _orig=self._orig_init):
            _orig(client_self, api_key=api_key, base_url=_fake_url, usage=usage)

        nvidia_mod.NvidiaClient.__init__ = patched_init

    def tearDown(self):
        self._nvidia.NvidiaClient.__init__ = self._orig_init
        self._fake.stop()

    def test_full_crew_chat_on_fake_server(self):
        from crew.site import SiteCrew
        crew = SiteCrew(api_key="nvapi-fake-key-for-tests")
        reply = crew.handle("Check my battery and tell me if I should"
                            " charge soon, please.")
        self.assertTrue(len(reply) > 10)
        self.assertGreater(crew.usage.total_tokens(), 0)

    def test_tool_route_reaches_operator_on_fake_server(self):
        """A tool-needing prompt routes to the Operator, which runs a real tool."""
        self._fake.script.insert(0, json_tool_call("battery", {}))
        self._fake.script.insert(1, '{"action": "answer",'
                                    ' "text": "Battery: 100% — charging."}')
        from crew.site import SiteCrew
        crew = SiteCrew(api_key="nvapi-fake-key-for-tests")
        reply = crew.handle("What is my battery level?")
        self.assertIn("attery", reply)


def json_tool_call(name, args):
    import json
    return json.dumps({"tool": name, "args": args})


if __name__ == "__main__":
    unittest.main(verbosity=2)
