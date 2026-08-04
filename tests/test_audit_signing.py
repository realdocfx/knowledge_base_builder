"""ML-DSA signing makes the audit chain unforgeable, not merely consistent (N3).

A SHA-256 hash chain proves *internal consistency*, but the hash function is
public: an adversary who can rewrite the log can recompute every digest and
re-chain a forged history that verifies clean. Signing each chain link with the
per-stick ML-DSA-65 keypair (:mod:`knowledge_base_builder.pqc`) closes that gap --
the forger can repair the hashes but cannot produce a matching signature without
the secret key.

The signature fields live outside the hashed canonical form, so they never feed
back into the hash they sign, and an unsigned log (no keypair, or no dilithium-py)
keeps chaining and verifying exactly as before.
"""

from __future__ import annotations

import base64
import json

import pytest

from knowledge_base_builder import audit, pqc

# The signed path needs the ML-DSA backend. The chain itself is a core control and
# is exercised unsigned in test_audit_log.py; here we prove the signing layer, so
# skip only this module when the backend is absent.
pytestmark = pytest.mark.skipif(
    not pqc.get_pqc_status().get("ml_dsa_65", False),
    reason="dilithium-py not installed; the signed-chain path cannot run",
)


@pytest.fixture()
def signed_log(tmp_path):
    """An AuditLog on a stick that already carries an ML-DSA signing identity."""
    pqc.generate_stick_keypair(tmp_path)
    return audit.AuditLog(tmp_path)


def _rewrite(log, entries) -> None:
    """Persist doctored *entries* back to the log file, in order."""
    lines = [json.dumps(e, sort_keys=True, default=str) for e in entries]
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------
def test_records_carry_a_verifiable_mldsa_signature(signed_log, tmp_path):
    signed_log.record("read", target="secret.pdf")
    entry = signed_log.read_all()[0]

    assert entry.get("sig_alg") == audit.SIG_ALG, entry
    assert entry.get("sig"), "record is unsigned despite a provisioned keypair"
    assert entry.get("sig_key"), "signed record does not name its signing key"

    pk, _sk = pqc.load_stick_keys(tmp_path)
    good = pqc.verify_signature(
        pk, entry["hash"].encode("ascii"), base64.b64decode(entry["sig"])
    )
    assert good, "the stored signature does not verify against the stick key"


def test_signature_fields_do_not_disturb_the_chain_hash(signed_log):
    """The signature must sign the hash, not be part of it (no circularity)."""
    signed_log.record("read", target="x")
    entry = signed_log.read_all()[0]
    # Recomputing the chain hash ignores sig/sig_alg/sig_key entirely.
    assert audit.compute_hash(entry) == entry["hash"]


def test_signed_chain_verifies(signed_log):
    for i in range(4):
        signed_log.record("acquire.complete", target=f"item{i}")
    ok, problem = signed_log.verify()
    assert ok, problem


# --------------------------------------------------------------------------
# The attack a bare hash chain cannot stop
# --------------------------------------------------------------------------
def test_a_consistently_rehashed_forgery_is_caught_by_the_signature(signed_log):
    """Edit a record, recompute its hash, and re-chain everything after it so the
    SHA-256 chain stays internally valid -- the forgery a public hash function
    cannot prevent. Only the signature, which the forger cannot reproduce, exposes
    it."""
    for target in ("innocuous", "routine", "mundane"):
        signed_log.record("read", target=target)
    entries = signed_log.read_all()

    # Forge record 2's target, then repair the hash chain around it exactly as an
    # attacker with write access -- but no secret key -- could.
    entries[1]["target"] = "exfil-to-adversary"
    entries[1]["hash"] = audit.compute_hash(entries[1])
    entries[2]["prev_hash"] = entries[1]["hash"]
    entries[2]["hash"] = audit.compute_hash(entries[2])
    _rewrite(signed_log, entries)

    # Document what the signature is buying: the hash chain ALONE now (wrongly)
    # reports the log as intact.
    doctored = signed_log.read_all()
    assert audit.compute_hash(doctored[1]) == doctored[1]["hash"]
    assert doctored[2]["prev_hash"] == doctored[1]["hash"]

    ok, problem = signed_log.verify()
    assert not ok, "a re-hashed forgery passed verify(); signing added nothing"
    assert "2" in problem and "signature" in problem.lower(), problem


# --------------------------------------------------------------------------
# Off-medium anchoring and key swap
# --------------------------------------------------------------------------
def test_key_swap_is_caught_by_an_off_medium_pubkey(signed_log, tmp_path):
    """An adversary who rewrites the log AND swaps the stick keypair passes the
    on-medium self-consistency check, but not verification against the original
    public key anchored off the medium."""
    signed_log.record("read", target="a")
    signed_log.record("read", target="b")
    original_pk, _ = pqc.load_stick_keys(tmp_path)

    # Swap in a fresh keypair and re-sign every record with it (content untouched).
    (tmp_path / pqc.PUBLIC_KEY_FILE).unlink()
    (audit.resolve_state_dir(tmp_path) / pqc.SIGNING_KEY_FILE).unlink()
    pqc.generate_stick_keypair(tmp_path)
    adv_pk, adv_sk = pqc.load_stick_keys(tmp_path)
    assert adv_pk != original_pk

    entries = signed_log.read_all()
    for e in entries:
        e["sig"] = base64.b64encode(
            pqc.sign_bytes(adv_sk, e["hash"].encode("ascii"))
        ).decode("ascii")
        e["sig_key"] = audit._pubkey_fingerprint(adv_pk)
    _rewrite(signed_log, entries)

    # Self-consistent under the swapped on-stick key -- the check an operator must
    # NOT rely on alone.
    ok, _ = signed_log.verify()
    assert ok, "a coherently re-signed log should pass the on-stick self-check"

    # Pinning the original public key exposes the swap.
    ok, problem = signed_log.verify(pubkey=original_pk)
    assert not ok, "a key swap slipped past an off-medium pubkey anchor"
    assert "key" in problem.lower(), problem


# --------------------------------------------------------------------------
# Backward compatibility and strict mode
# --------------------------------------------------------------------------
def test_unsigned_log_still_chains_and_verifies(tmp_path):
    """No keypair -> records are written unsigned and the chain verifies as before."""
    log = audit.AuditLog(tmp_path)  # no generate_stick_keypair
    for i in range(3):
        log.record("read", target=f"f{i}")
    entries = log.read_all()
    assert all("sig" not in e for e in entries), "signed without a keypair present"
    ok, problem = log.verify()
    assert ok, problem


def test_require_signatures_rejects_an_unsigned_record(tmp_path):
    log = audit.AuditLog(tmp_path)  # unsigned records
    log.record("read", target="x")
    ok, _ = log.verify()
    assert ok, "the chain itself is valid"
    ok, problem = log.verify(require_signatures=True)
    assert not ok and "require" in problem.lower(), problem


def test_a_stripped_signature_is_caught_when_signatures_are_required(signed_log):
    signed_log.record("read", target="a")
    signed_log.record("read", target="b")
    entries = signed_log.read_all()
    for field in ("sig", "sig_alg", "sig_key"):
        entries[1].pop(field, None)
    _rewrite(signed_log, entries)

    # A plain verify accepts it (looks like a legitimately unsigned record)...
    ok, _ = signed_log.verify()
    assert ok
    # ...but strict mode flags the record whose signature was stripped.
    ok, problem = signed_log.verify(require_signatures=True)
    assert not ok and "2" in problem, problem


# --------------------------------------------------------------------------
# Provisioning wires the identity in
# --------------------------------------------------------------------------
def test_provisioning_generates_a_signing_identity(tmp_path):
    from knowledge_base_builder import cli

    cli._provision_signing_identity(tmp_path)
    pk, sk = pqc.load_stick_keys(tmp_path)
    assert pk and sk, "provisioning did not create a signing identity"

    log = audit.AuditLog(tmp_path)
    log.record("config.change", target="x")
    assert log.read_all()[0].get("sig"), "provisioned stick still writes unsigned"
