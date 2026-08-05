"""The WebView-shell app must *request* Termux's RUN_COMMAND permission at runtime.

Termux declares `com.termux.permission.RUN_COMMAND` as a **dangerous** permission and
protects its `RunCommandService` with it. A dangerous permission installs
`granted=false`, so merely listing it in the manifest is not enough: without an
explicit runtime request the auto-start `startForegroundService()` call throws
`SecurityException`, the app silently catches it and drops to the manual buttons --
which is precisely the observed "auto-start doesn't work" failure on the phone.

These are static guards over the checked-in Android sources (this build host has no
Android toolchain/emulator). They fail on a declare-but-never-request regression.
"""

from __future__ import annotations

from pathlib import Path

_MAIN = Path(__file__).resolve().parent.parent / "android" / "app" / "src" / "main"
_MANIFEST = _MAIN / "AndroidManifest.xml"
_ACTIVITY = _MAIN / "java" / "org" / "kbb" / "portal" / "MainActivity.kt"

_PERM = "com.termux.permission.RUN_COMMAND"


def test_manifest_declares_run_command_permission():
    assert _PERM in _MANIFEST.read_text(encoding="utf-8")


def test_app_checks_and_requests_run_command_permission_at_runtime():
    src = _ACTIVITY.read_text(encoding="utf-8")
    assert _PERM in src, "the permission constant must appear in the activity"
    # It must query whether the dangerous permission is held...
    assert "checkSelfPermission" in src, (
        "must check the RUN_COMMAND grant before firing the intent"
    )
    # ...and actually prompt for it when it is not.
    assert "requestPermissions" in src, (
        "a dangerous permission installs granted=false; the app must request it"
    )


def test_app_resumes_start_flow_after_permission_result():
    src = _ACTIVITY.read_text(encoding="utf-8")
    assert "onRequestPermissionsResult" in src, (
        "the grant callback must re-enter the backend start flow so the newly granted "
        "permission actually launches the portal"
    )
