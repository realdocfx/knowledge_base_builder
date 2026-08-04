"""The Android (Termux) bundle stages the runtime + crypto tokens + a content
manifest, and never duplicates the big ZIM/media content.

The phone runs the same pure-Python KBB portal on ARM64; the only things that must
travel besides the (separately copied) content are the setup scripts and the crypto
tokens that let the SAME passphrase re-derive the key and decrypt the media.
"""

from __future__ import annotations

import pytest

from knowledge_base_builder import at_rest, cli, pqc

pytestmark = pytest.mark.skipif(
    not pqc.get_pqc_status().get("aes_256_gcm", False),
    reason="AES-256-GCM backend (cryptography) absent",
)


def test_android_bundle_stages_scripts_tokens_and_manifest(tmp_path, monkeypatch):
    bucket = tmp_path / "stick"
    arch = bucket / "library" / "archive"
    (arch / ".kb_state").mkdir(parents=True)
    (arch / ".kb_state" / pqc.CRYPTO_SALT_FILE).write_bytes(b"salt-sixteen-byt")
    (arch / ".kb_state" / pqc.CRYPTO_VERIFY_FILE).write_bytes(b"verify-token-blob")
    # The signing key must NOT be copied (per-device identity).
    (arch / ".kb_state" / pqc.SIGNING_KEY_FILE).write_bytes(b"secret-signing-key")

    # A split ZIM (two slices) + one encrypted media file.
    (arch / "wikipedia_fr_top_maxi.zimaa").write_bytes(b"\x00" * 100)
    (arch / "wikipedia_fr_top_maxi.zimab").write_bytes(b"\x00" * 100)
    (arch / "book").mkdir()
    key = pqc.generate_salt() + pqc.generate_salt()
    (arch / "book" / "doc.pdf").write_bytes(b"%PDF sensitive media body")
    at_rest.encrypt_file(key, arch / "book" / "doc.pdf")

    # Skip the (slow) real wheel build.
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)

    dest = tmp_path / "bundle"
    cli.android_bundle(str(dest), from_bucket=str(bucket))

    # Scripts present, shell shebang, LF line endings.
    setup = dest / "termux-setup.sh"
    assert setup.is_file()
    assert setup.read_bytes().startswith(b"#!/data/data/com.termux")
    assert b"\r\n" not in setup.read_bytes(), "scripts must use LF, not CRLF"
    assert (dest / "kbb-start.sh").is_file()

    # Crypto tokens staged; signing key NOT copied.
    assert (dest / "kb_state" / pqc.CRYPTO_SALT_FILE).is_file()
    assert (dest / "kb_state" / pqc.CRYPTO_VERIFY_FILE).is_file()
    assert not (dest / "kb_state" / pqc.SIGNING_KEY_FILE).exists(), \
        "the per-device signing key must not travel to another device"

    # Manifest lists the ZIM (reassembled name) + the media count, and no big content
    # was copied into the bundle.
    manifest = (dest / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "wikipedia_fr_top_maxi.zim" in manifest
    assert "1 files" in manifest
    assert not (dest / "book").exists(), "media must not be duplicated into the bundle"
    assert not list(dest.glob("*.zim*")), "ZIM slices must not be duplicated into the bundle"
