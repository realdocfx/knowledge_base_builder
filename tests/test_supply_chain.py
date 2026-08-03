"""Supply-chain controls must exist, not merely be advertised.

This is the finding an evaluator can check in ten minutes without reading code,
which is why it ends a procurement conversation fastest. The repository *presented*
a posture -- hash-pinned requirements, an SBOM, an air-gap flag, "MIL-SPEC
COMPLIANCE" comments -- while:

* **D8** ``_install_portable_packages`` claimed in its own docstring to use
  "requirements.txt with SHA-256 hashes" and then ran bare
  ``pip install fastapi>=... uvicorn[standard]>=... httpx>=...``. No ``-r``, no
  ``--require-hashes``, no index pinning. The 57 KB hash-pinned requirements file
  was never consulted at provisioning time.
* **D29** the publish workflow combined ``id-token: write`` and an ``environment:``
  (trusted publishing) with an explicit ``password:``, which **disables** OIDC and
  silently reverts to a long-lived token.
* **D31** every ``uses:`` referenced a mutable ``@vN`` tag, so the build that
  enforces provenance on downloads enforced none on itself.
* No SLSA provenance attestation for any released artefact.

A claim the code does not implement is worse than an absent claim: an evaluator who
checks one and finds it hollow discounts the whole evidence package.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from knowledge_base_builder import cli

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = sorted((_ROOT / ".github" / "workflows").glob("*.yml"))


# --------------------------------------------------------------------------
# D8 -- provisioning must actually verify what it installs
# --------------------------------------------------------------------------
def test_dependency_install_uses_hash_pinned_requirements():
    """The hash-pinned requirements file must be used, not just referenced in prose."""
    source = inspect.getsource(cli._install_portable_packages)
    assert "--require-hashes" in source, (
        "provisioning installs dependencies without --require-hashes, so the "
        "hash-pinned requirements.txt provides no protection at provisioning time"
    )
    assert "-r" in source and "requirements" in source, (
        "provisioning must install from the requirements file, not from loose "
        "version ranges"
    )


def test_no_unpinned_version_ranges_are_installed():
    """Loose ranges resolve to whatever PyPI serves today; that is not provenance."""
    source = inspect.getsource(cli._install_portable_packages)
    ranges = re.findall(r'"[a-zA-Z0-9_.\[\]-]+>=[0-9][^"]*"', source)
    assert not ranges, f"unpinned requirement specifiers still installed: {ranges}"


def test_docstring_does_not_claim_more_than_the_code_does():
    """If the code stops hash-checking, the docstring must not still promise it."""
    doc = inspect.getdoc(cli._install_portable_packages) or ""
    source = inspect.getsource(cli._install_portable_packages)
    if re.search(r"hash", doc, re.I):
        assert "--require-hashes" in source, (
            "docstring advertises hash verification that the implementation does "
            "not perform"
        )


def test_airgapped_install_pins_the_index():
    """With a local bundle, resolution must not fall through to PyPI (D9)."""
    source = inspect.getsource(cli._install_portable_packages)
    assert "--no-index" in source, (
        "air-gapped provisioning must pass --no-index, or the dependency install "
        "still reaches PyPI and the air-gap claim is false"
    )
    assert "--find-links" in source or "--no-index" in source


# --------------------------------------------------------------------------
# D31 -- the build must pin its own tools
# --------------------------------------------------------------------------
@pytest.mark.parametrize("workflow", _WORKFLOWS, ids=lambda p: p.name)
def test_actions_are_pinned_to_commit_shas(workflow):
    """A mutable tag lets an upstream change alter our build without review."""
    text = workflow.read_text(encoding="utf-8")
    unpinned = re.findall(r"uses:\s*([^\s@]+@(?!\b[0-9a-f]{40}\b)[^\s#]+)", text)
    # Actions published by GitHub itself under the same org as the runner are still
    # third-party code; no exemptions.
    assert not unpinned, (
        f"{workflow.name} references mutable action refs: {unpinned}. Pin to a "
        "40-character commit SHA (a trailing '# vX' comment keeps it readable)."
    )


# --------------------------------------------------------------------------
# D29 -- OIDC or a token, never both
# --------------------------------------------------------------------------
def test_publish_uses_trusted_publishing_without_a_static_token():
    publish = _ROOT / ".github" / "workflows" / "publish.yml"
    if not publish.is_file():
        pytest.skip("no publish workflow")
    text = publish.read_text(encoding="utf-8")
    if "id-token: write" in text:
        assert not re.search(r"^\s*password:", text, re.M), (
            "supplying `password:` disables OIDC trusted publishing and reverts to "
            "a long-lived token, defeating the reason id-token: write was requested"
        )


# --------------------------------------------------------------------------
# SLSA provenance
# --------------------------------------------------------------------------
def test_released_artefacts_carry_a_provenance_attestation():
    publish = _ROOT / ".github" / "workflows" / "publish.yml"
    if not publish.is_file():
        pytest.skip("no publish workflow")
    text = publish.read_text(encoding="utf-8")
    assert "attest-build-provenance" in text, (
        "no SLSA provenance is generated, so a compromised release asset cannot be "
        "distinguished from a legitimate one"
    )
    assert "attestations: write" in text, (
        "provenance attestation requires the attestations: write permission"
    )


def test_sbom_is_attached_to_releases():
    """An SBOM nobody can retrieve alongside the artefact is not evidence."""
    workflows = " ".join(w.read_text(encoding="utf-8") for w in _WORKFLOWS)
    assert "sbom.json" in workflows, "the SBOM is never published with a release"


# --------------------------------------------------------------------------
# P3: the guest image is a shipping artefact -- its own fetches must be verified.
# The suite previously inspected only cli._install_portable_packages, so it could
# not observe the guest-image build in sandbox.yml.
# --------------------------------------------------------------------------
def test_guest_image_verifies_its_kiwix_fetch():
    yml = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "sandbox.yml").read_text(
        encoding="utf-8"
    )
    assert "kiwix-tools_linux-x86_64-musl" in yml, "kiwix-tools fetch not found (test stale?)"
    assert re.search(r"KIWIX_SHA256=[0-9a-f]{64}", yml), (
        "the guest-image kiwix-tools fetch is not pinned to a SHA-256 (audit P3)"
    )
    assert "sha256sum -c" in yml, (
        "the guest-image kiwix-tools download is not verified with `sha256sum -c` (audit P3)"
    )
