"""Conformance of the published SBOM.

The committed ``sbom.json`` failed the NTIA minimum elements on three counts and
had been unbuildable for weeks:

* CycloneDX **1.3** (current is 1.6).
* No ``metadata.component`` -- the document did not identify its own subject, so
  a consumer could not tell what the BOM described.
* No ``dependencies`` graph -- component relationships were absent.
* **Zero** ``licenses`` across 36 components.
* Corrupt hash values such as ``"571ac...320 \\"`` -- a ``requirements.txt``
  line-continuation swept into the digest field.

The generator itself had also rotted: it invoked ``cyclonedx-py`` with pre-v4
flags (``--outfile/--format/--schema-version``), so the workflow failed on every
push, and it injected a hardcoded version string that had drifted from the real
one.

These assertions are what "conformant" means for this project, checked against
the artefact that actually ships.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SBOM = _ROOT / "sbom.json"

pytestmark = pytest.mark.skipif(
    not _SBOM.is_file(), reason="sbom.json not present in this checkout"
)


@pytest.fixture(scope="module")
def sbom() -> dict:
    return json.loads(_SBOM.read_text(encoding="utf-8"))


def _declared_version() -> str:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "project version not found in pyproject.toml"
    return m.group(1)


def test_schema_version_is_current(sbom):
    """CycloneDX 1.6 or newer."""
    spec = sbom.get("specVersion", "0")
    major, _, minor = spec.partition(".")
    assert (int(major), int(minor or 0)) >= (1, 6), f"specVersion is {spec}, expected >= 1.6"


def test_sbom_identifies_its_own_subject(sbom):
    """metadata.component must name this project at its real, current version."""
    mc = sbom.get("metadata", {}).get("component")
    assert mc, "metadata.component absent: the SBOM does not identify its subject"
    assert mc.get("name") == "knowledge-base-builder", mc
    assert mc.get("version") == _declared_version(), (
        f"SBOM subject version {mc.get('version')!r} has drifted from "
        f"pyproject {_declared_version()!r}"
    )


def test_dependency_graph_is_present(sbom):
    """Relationships, not just an inventory."""
    deps = sbom.get("dependencies")
    assert deps, "no dependencies graph: component relationships are unexpressed"
    assert len(deps) > 1


def test_every_component_declares_a_licence(sbom):
    """Licensing is an NTIA minimum element."""
    comps = sbom.get("components", [])
    assert comps, "SBOM lists no components"
    missing = sorted(c.get("name", "?") for c in comps if not c.get("licenses"))
    assert not missing, (
        f"{len(missing)} of {len(comps)} components declare no licence: "
        f"{missing[:12]}{' ...' if len(missing) > 12 else ''}"
    )


def test_hashes_are_well_formed(sbom):
    """A digest must be hex of the right width -- not a mangled requirements line."""
    bad = []
    widths = {"MD5": 32, "SHA-1": 40, "SHA-256": 64, "SHA-384": 96, "SHA-512": 128}
    for comp in sbom.get("components", []):
        for h in comp.get("hashes", []):
            content = h.get("content", "")
            alg = h.get("alg", "")
            if not re.fullmatch(r"[0-9a-fA-F]+", content or ""):
                bad.append(f"{comp.get('name')}: non-hex {content!r}")
            elif alg in widths and len(content) != widths[alg]:
                bad.append(f"{comp.get('name')}: {alg} width {len(content)}")
    assert not bad, "malformed hash entries:\n  " + "\n  ".join(bad[:12])


# --------------------------------------------------------------------------
# N5: the well-formed-hash test above looped over an EMPTY list -- no component
# carried a digest, so it passed vacuously. These assert the hashes exist and
# cover the pinned runtime closure.
# --------------------------------------------------------------------------
def test_every_component_carries_a_hash(sbom):
    missing = [c.get("name") for c in sbom.get("components", []) if not c.get("hashes")]
    assert not missing, (
        f"{len(missing)} SBOM components carry no hash (audit N5): {missing[:12]}"
    )


def test_sbom_covers_the_pinned_requirements(sbom):
    """Every hash-pinned runtime package must appear as an SBOM component."""
    pinned = set()
    for line in (_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==", line)
        if m:
            pinned.add(m.group(1).lower().replace("_", "-"))
    present = set()
    for c in sbom.get("components", []):
        purl = c.get("purl", "")
        name = purl.split("/")[-1].split("@")[0] if purl.startswith("pkg:pypi/") else c.get("name", "")
        present.add(name.lower().replace("_", "-"))
    missing = pinned - present
    assert not missing, f"pinned dependencies absent from the SBOM (audit N5): {sorted(missing)}"
