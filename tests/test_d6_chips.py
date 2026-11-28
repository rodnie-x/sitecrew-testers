"""D6 characterization (boss ruling): Used chips attach ONLY to successful turns.

A failed turn (crew error) must NEVER carry tools_used — the chip promise is
'what ran AND verified', so a failed job shows no chip. This pins the corrected
behavior that Phase 2/3 will implement (server/app.py currently attaches counts
in the finally block regardless of outcome — D6 fix lands with the engine).
"""
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CREW_PRESENT = importlib.util.find_spec("crew") is not None
if CREW_PRESENT:
    from tests.helpers import reset_state, make_crew  # noqa: E402
    import server.app as module  # noqa: E402
else:
    reset_state = make_crew = None
    module = None
client = module.app.test_client() if module is not None else None


@unittest.skipUnless(CREW_PRESENT, "full product modules are not in the public subset")
class D6ChipHonesty(unittest.TestCase):
    def setUp(self):
        reset_state(monkey_net=True, fake_crew=make_crew())

    def tearDown(self):
        reset_state(monkey_net=False)

    def test_successful_job_carries_chip(self):
        module._net_ok = lambda max_age=10.0: True
        client.post("/api/chat", json={"message": "run the battery tool"})
        last = client.get("/api/state").get_json()["messages"][-1]
        self.assertEqual(last.get("tools_used", {}).get("battery"), 1)

    def test_failed_job_must_not_carry_chip(self):
        """D6 target behavior: crew error → no tools_used on the reply.

        Current implementation (v1) attaches counts in the finally block even on
        failure, so this asserts the CORRECTED contract via a marker the engine
        will honor: the reply content starts with 'Crew error:' → chip absent.
        Characterization first: if this fails now, it is the known D6 gap
        (docs/DEVIATIONS.md), scheduled for Phase 2/3.
        """
        import threading

        def failing_handle(msg, history):
            self.store = module
            raise RuntimeError("boom — the crew failed")

        module._net_ok = lambda max_age=10.0: True
        module.crew = make_crew()
        module.crew.handle = failing_handle
        module.build_crew = lambda: True
        client.post("/api/chat", json={"message": "break please"})
        last = client.get("/api/state").get_json()["messages"][-1]
        content = last.get("content", "")
        if content.startswith("Crew error:"):
            # D6 gap: engine still attaches counts on failure — assert the chip
            # is HONEST about verification: a failed job has NO verified chip.
            # Phase 2 fixes the engine; here we pin that the chip must vanish.
            self.assertNotIn(
                "tools_used", last,
                "D6: failed jobs must not display a Used chip")
        else:
            self.assertNotIn("tools_used", last)


if __name__ == "__main__":
    unittest.main(verbosity=2)
