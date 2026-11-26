# TEST_TARGETS.md — throwaway targets for real sensitive-action tests on the phone

Per spec §11.1: without these values the coding agent tests sensitive paths with fakes
only and marks device-effect criteria **NOT RUN**. Fill the values you control, then
device-effect gates may run for those tools only.

```yaml
# --- fill these in (values only you can supply) ---
sms_test_number: ""          # a phone number YOU control that can receive test SMS
call_test_number: ""         # a number YOU control; never call emergency numbers
uninstall_test_package: ""   # a throwaway APK installed on the phone just for this
settings_test: brightness    # spec default: brightness only (fully reversible)

# --- Real Office addendum (blueprint §7) ---
throwaway_workspace: "/storage/emulated/0/Documents/SiteCrew-test"   # throwaway Workspace folder for fs.* tests (created in onboarding or by the office)
hq_test_folder: ""           # a laptop test folder for HQ file tests (e.g. ~/Projects/sitecrew-test)

# --- reserved for later phases ---
share_test_text: "SiteCrew share-in test"
reminder_test_minutes: 1
```

Rules (from the spec master prompt):

- Real sensitive actions run ONLY against these targets, never real contacts/apps.
- `settings_test` stays `brightness` — other settings are out of scope for tests.
- If a value is empty, that tool's device-effect criterion is **NOT RUN** with this reason.
- Approvals always gate the action; a test that skips the gate is a spec violation.
