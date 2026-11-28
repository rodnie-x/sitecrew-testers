"""Compile-time build flags for SiteCrew AI (frozen edit set member).

docs/DEVICE_CPU_DIAG_SPEC.md §1: ``build_flags.py`` — ``DEBUG_ENDPOINTS`` as a
compile-time constant. This file joins the frozen hash list (hashed into every
diag header alongside the other frozen modules).

Semantics (boss ruling):

- The value below is edited BEFORE building/hot-deploying a debug or diagnostic
  APK and is read at import time only. It is deliberately NOT read from env,
  marker files, or settings — a release APK must never grow debug endpoints by
  accident, and a running process must never be able to switch them on.
- ``False`` (the shipped default) = diagnostic/debug routes are never
  registered: requests to them 404 and ``cpu_diag`` is never imported.
- ``True`` = register the minimal flag-gated diag loopback endpoints
  (``/api/v2/debug/diag/*``) on the next process start.

Flipping this constant changes this file's SHA-256, which every CPU-diagnostic
header records — so the flag state of a run is always provable from the run's
own evidence. (The pre-existing env/marker gate for the OLDER
``/api/v2/debug/*`` store endpoints in server/app.py is unchanged and
independent.)
"""

DEBUG_ENDPOINTS = False
