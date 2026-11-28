"""Capture or verify the deterministic P1.5 legacy GET goldens.

Capture (writes tests/fixtures/legacy_get):
    python scripts/capture_legacy_get.py

Verify without writing:
    python scripts/capture_legacy_get.py --check

Only Flask's test client and the existing characterization fake seams are used.
Response bodies are always compared byte-for-byte. Date, ETag and Last-Modified
are the only ignored response metadata: they depend on wall time or file mtimes,
not on the legacy response representation.
"""
import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.helpers import make_crew, reset_state  # noqa: E402
import server.app as module  # noqa: E402

FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures", "legacy_get")
BODY_DIR = os.path.join(FIXTURE_DIR, "bodies")
MANIFEST_PATH = os.path.join(FIXTURE_DIR, "manifest.json")
STATIC_DIR = os.path.join(ROOT, "server", "static")

# This object is part of the golden contract. Do not add a rule to make a later
# implementation pass: intentionally change the contract only with new baseline
# review, and update the independent hash pin in test_legacy_get_golden.py.
NORMALIZATION = {
    "version": 1,
    "ignored_headers": {
        "Date": "HTTP response wall clock",
        "ETag": "Flask file validator incorporates the source file mtime",
        "Last-Modified": "source file mtime is not stable across checkouts",
    },
    "body_replacements": [],
    "json_pointer_replacements": [],
    "body_policy": "exact bytes; event time and busy_since are frozen by fixtures",
}


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def static_routes():
    """The root plus every existing regular file exposed by static_url_path=''."""
    names = sorted(name for name in os.listdir(STATIC_DIR)
                   if os.path.isfile(os.path.join(STATIC_DIR, name)))
    return ["/"] + ["/" + name for name in names]


def state_fixture_names():
    return [
        "empty",
        "normal_full_metadata",
        "queued",
        "attachment_model_content_stripped",
        "restored",
        "messages_over_60",
        "activity_over_100",
        "busy_offline",
    ]


def fixture_definitions():
    """Serializable fixture contract stored in, and checked against, the manifest."""
    definitions = [
        {
            "id": "static__root" if route == "/" else "static__" + route[1:].replace(".", "_"),
            "kind": "static",
            "method": "GET",
            "route": route,
            "source": "server/static/index.html" if route == "/" else "server/static" + route,
        }
        for route in static_routes()
    ]
    controls = {
        "empty": {"network": False, "crew": None},
        "normal_full_metadata": {
            "network": False,
            "crew": "tests.helpers.make_crew",
            "usage": {"fake/small": {"calls": 2, "prompt_tokens": 11, "completion_tokens": 7}},
            "messages": 2,
            "activity": 2,
            "approvals": 1,
        },
        "queued": {
            "network": False,
            "mutation": "POST /api/chat with one deterministic offline message",
            "event_time_freeze": "12:34:56",
            "queue_file": "temporary directory",
        },
        "attachment_model_content_stripped": {
            "network": False,
            "mutation": "prepare_attachments + _user_message",
            "private_fields_expected_on_server": ["model_content"],
            "private_fields_forbidden_in_response": ["model_content", "attachments[].text"],
        },
        "restored": {
            "network": False,
            "mutation": "POST /api/restore with hostile model_content and attachment text",
        },
        "messages_over_60": {"network": False, "input_messages": 65, "expected_messages": 60},
        "activity_over_100": {"network": False, "input_activity": 125, "expected_activity": 100},
        "busy_offline": {"network": False, "busy": True, "busy_since_freeze": 1700000000.25},
    }
    definitions.extend({
        "id": "api_state__" + name,
        "kind": "api_state",
        "method": "GET",
        "route": "/api/state",
        "fixture": name,
        "controls": controls[name],
    } for name in state_fixture_names())
    return definitions


def _fixed_activity(index, prefix="event"):
    return {
        "worker": "foreman" if index % 2 == 0 else "laborer",
        "model": "fake/small",
        "action": "%s-%03d" % (prefix, index),
        "detail": "deterministic detail %03d" % index,
        "tokens": index,
        "level": "info",
        "time": "12:%02d:%02d" % (index // 60 % 60, index % 60),
    }


def _setup_state_fixture(name, client):
    reset_state(monkey_net=False)
    module.get_key = lambda: ""

    if name == "empty":
        return

    if name == "normal_full_metadata":
        crew = make_crew("metadata reply")
        crew.usage = type("FixedUsage", (), {"to_dict": staticmethod(lambda: {
            "fake/small": {"calls": 2, "prompt_tokens": 11, "completion_tokens": 7},
        })})()
        reset_state(monkey_net=False, fake_crew=crew)
        module.STATE["messages"] = [
            {"role": "user", "content": "normal question"},
            {"role": "assistant", "content": "normal answer", "job": 1,
             "elapsed_s": 1.25, "workers": ["foreman"], "tools_used": {"battery": 1}},
        ]
        module.STATE["activity"].extend([_fixed_activity(0), _fixed_activity(1)])
        module.STATE["approvals"] = {
            "approval-fixed": {"id": "approval-fixed", "tool": "set_setting",
                               "summary": "brightness -> 50", "time": "12:34:56",
                               "private": "must not leave the state endpoint"},
        }
        return

    if name == "queued":
        with mock.patch("crew.models.now_stamp", return_value="12:34:56"):
            response = client.post("/api/chat", json={"message": "deterministic queued request"})
        if response.status_code != 200 or not response.get_json().get("queued"):
            raise RuntimeError("queued fixture did not use the legacy queue path: %r" % response.data)
        return

    if name == "attachment_model_content_stripped":
        item = {
            "name": "private.txt",
            "type": "text/plain",
            "data": base64.b64encode(b"server only attachment text").decode("ascii"),
        }
        public, context = module.prepare_attachments([item])
        module.STATE["messages"] = [
            module._user_message("summarize the attachment", public, context),
            {"role": "assistant", "content": "public answer"},
        ]
        return

    if name == "restored":
        response = client.post("/api/restore", json={"messages": [
            {"role": "user", "content": "restored question", "model_content": "DO NOT LEAK",
             "attachments": [{"name": "saved.txt", "size": 4, "type": "text/plain",
                              "characters": 4, "truncated": False, "text": "DO NOT LEAK"}]},
            {"role": "assistant", "content": "restored answer", "model_content": "DO NOT LEAK"},
        ]})
        if response.status_code != 200 or response.get_json().get("count") != 2:
            raise RuntimeError("restored fixture failed: %r" % response.data)
        return

    if name == "messages_over_60":
        module.STATE["messages"] = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": "message-%03d" % i, "sequence": i}
            for i in range(65)
        ]
        return

    if name == "activity_over_100":
        module.STATE["activity"].extend(_fixed_activity(i, "overflow") for i in range(125))
        return

    if name == "busy_offline":
        module.STATE["busy"] = True
        module.STATE["busy_since"] = 1700000000.25
        module.STATE["messages"] = [{"role": "user", "content": "work in progress"}]
        return

    raise ValueError("unknown state fixture: " + name)


def normalize_headers(headers):
    ignored = {name.lower() for name in NORMALIZATION["ignored_headers"]}
    return {key: value for key, value in headers.items() if key.lower() not in ignored}


def capture_responses():
    """Return a complete in-memory capture without changing repository fixtures."""
    definitions = fixture_definitions()
    records = []
    old_queue_file = module.QUEUE_FILE
    old_net_ok = module._net_ok
    old_get_key = module.get_key
    old_store_flag = os.environ.get("SITECREW_STORE")
    temp_dir = tempfile.mkdtemp(prefix="sitecrew-legacy-get-")
    client = module.app.test_client()
    try:
        module.QUEUE_FILE = os.path.join(temp_dir, "queue.json")
        os.environ.pop("SITECREW_STORE", None)
        for definition in definitions:
            reset_state(monkey_net=False)
            module.get_key = lambda: ""
            if definition["kind"] == "api_state":
                _setup_state_fixture(definition["fixture"], client)
            response = client.get(definition["route"])
            body = response.get_data()
            record = {
                "id": definition["id"],
                "method": "GET",
                "route": definition["route"],
                "fixture": definition.get("fixture"),
                "status": response.status_code,
                "headers": normalize_headers(response.headers),
                "body_file": "bodies/" + definition["id"] + ".bin",
                "body_bytes": len(body),
                "body_sha256": sha256_bytes(body),
            }
            records.append((record, body))
            response.close()
        reset_state(monkey_net=False)
    finally:
        reset_state(monkey_net=False)
        module.QUEUE_FILE = old_queue_file
        module._net_ok = old_net_ok
        module.get_key = old_get_key
        if old_store_flag is None:
            os.environ.pop("SITECREW_STORE", None)
        else:
            os.environ["SITECREW_STORE"] = old_store_flag
        shutil.rmtree(temp_dir, ignore_errors=True)

    normalization_sha256 = sha256_bytes(canonical_bytes(NORMALIZATION))
    definitions_sha256 = sha256_bytes(canonical_bytes(definitions))
    manifest = {
        "format": 1,
        "purpose": "P1.5 pre-route legacy GET baseline; no mirror or flip",
        "legacy_get_count": len(records),
        "api_state_fixture_count": len(state_fixture_names()),
        "static_get_count": len(records) - len(state_fixture_names()),
        "normalization": NORMALIZATION,
        "normalization_sha256": normalization_sha256,
        "fixture_definitions": definitions,
        "fixture_definitions_sha256": definitions_sha256,
        "responses": [record for record, _body in records],
    }
    return manifest, records


def write_capture():
    manifest, records = capture_responses()
    os.makedirs(BODY_DIR, exist_ok=True)
    expected_files = set()
    for record, body in records:
        relative = record["body_file"]
        expected_files.add(os.path.normpath(os.path.join(FIXTURE_DIR, relative)))
        path = os.path.join(FIXTURE_DIR, relative)
        with open(path, "wb") as output:
            output.write(body)
    for name in os.listdir(BODY_DIR):
        path = os.path.normpath(os.path.join(BODY_DIR, name))
        if path not in expected_files and os.path.isfile(path):
            os.remove(path)
    temporary = MANIFEST_PATH + ".tmp"
    with open(temporary, "wb") as output:
        output.write(json.dumps(manifest, ensure_ascii=False, indent=2,
                                sort_keys=True).encode("utf-8") + b"\n")
    os.replace(temporary, MANIFEST_PATH)
    return manifest


def load_capture():
    with open(MANIFEST_PATH, "rb") as source:
        manifest_bytes = source.read()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    records = []
    for record in manifest["responses"]:
        with open(os.path.join(FIXTURE_DIR, record["body_file"]), "rb") as source:
            body = source.read()
        records.append((record, body))
    return manifest, records, manifest_bytes


def check_capture():
    expected, expected_records, _manifest_bytes = load_capture()
    actual, actual_records = capture_responses()
    errors = []
    for label, pinned, current in (
        ("normalization rules", expected["normalization_sha256"],
         sha256_bytes(canonical_bytes(NORMALIZATION))),
        ("fixture definitions", expected["fixture_definitions_sha256"],
         sha256_bytes(canonical_bytes(fixture_definitions()))),
    ):
        if pinned != current:
            errors.append("%s drift: manifest=%s current=%s" % (label, pinned, current))
    if expected["normalization"] != NORMALIZATION:
        errors.append("manifest normalization object differs from declared rules")
    if expected["fixture_definitions"] != fixture_definitions():
        errors.append("manifest fixture definitions differ from current definitions")
    expected_by_id = {record["id"]: (record, body) for record, body in expected_records}
    actual_by_id = {record["id"]: (record, body) for record, body in actual_records}
    if set(expected_by_id) != set(actual_by_id):
        errors.append("captured surface IDs differ from manifest")
    for case_id in sorted(set(expected_by_id) & set(actual_by_id)):
        wanted, wanted_body = expected_by_id[case_id]
        got, got_body = actual_by_id[case_id]
        for field in ("method", "route", "fixture", "status", "headers"):
            if wanted[field] != got[field]:
                errors.append("%s %s differs: %r != %r" %
                              (case_id, field, wanted[field], got[field]))
        if wanted_body != got_body:
            errors.append("%s raw body differs: expected %s, got %s" %
                          (case_id, sha256_bytes(wanted_body), sha256_bytes(got_body)))
        if wanted["body_sha256"] != sha256_bytes(wanted_body):
            errors.append("%s fixture body does not match manifest hash" % case_id)
        if wanted["body_bytes"] != len(wanted_body):
            errors.append("%s fixture body does not match manifest length" % case_id)
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without rewriting goldens")
    args = parser.parse_args(argv)
    if args.check:
        errors = check_capture()
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        manifest, _records, _manifest_bytes = load_capture()
        print("PASS: %d legacy GET goldens (%d /api/state, %d static), exact raw bodies" %
              (manifest["legacy_get_count"], manifest["api_state_fixture_count"],
               manifest["static_get_count"]))
        return 0
    manifest = write_capture()
    print("Captured %d legacy GET goldens (%d /api/state, %d static) in %s" %
          (manifest["legacy_get_count"], manifest["api_state_fixture_count"],
           manifest["static_get_count"], FIXTURE_DIR))
    print("Normalization SHA256: " + manifest["normalization_sha256"])
    print("Fixture definitions SHA256: " + manifest["fixture_definitions_sha256"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
