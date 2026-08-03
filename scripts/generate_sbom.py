#!/usr/bin/env python3
"""Generate a conformant CycloneDX SBOM for Knowledge-Base-Builder.

Why this is not simply ``cyclonedx-py requirements requirements.txt``:

* ``requirements.txt`` carries names and hashes but no licence metadata, and
  licensing is an NTIA minimum element. Scanning a *resolved environment* is the
  only way to obtain it.
* Scanning the developer's ambient environment is equally wrong -- it reports
  whatever happens to be installed (pytest, ruff, build tooling) as though it
  were part of the product.

So this builds a throwaway virtual environment containing exactly the project and
its runtime dependencies, and scans that. The result carries the correct closure,
real licences, a dependency graph, and -- via ``--pyproject`` -- a
``metadata.component`` naming the subject at the version declared in
``pyproject.toml`` rather than a hardcoded string that drifts (the previous
version of this script asserted 0.4.3 while the project shipped 0.5.0).

It also fixes the reason this had been failing on every push for weeks: the old
invocation used pre-v4 flags (``--outfile``/``--format``/``--schema-version``),
which current ``cyclonedx-py`` rejects outright.

Conformance is asserted by tests/test_sbom_conformance.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SPEC_VERSION = "1.6"


def _venv_python(venv_dir: Path) -> Path:
    """Interpreter path inside *venv_dir*, for either platform layout."""
    for rel in ("bin/python", "Scripts/python.exe"):
        candidate = venv_dir / rel
        if candidate.exists():
            return candidate
    raise RuntimeError(f"no interpreter found in {venv_dir}")


def _run(cmd: list, what: str) -> None:
    logger.info("%s ...", what)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("FAILED: %s", what)
        logger.error("command: %s", " ".join(str(c) for c in cmd))
        logger.error("stderr:\n%s", (result.stderr or "").strip()[-4000:])
        raise SystemExit(1)


def generate(project_root: Path, output: Path) -> None:
    """Build an isolated environment from the project and emit its SBOM."""
    with tempfile.TemporaryDirectory(prefix="kbb-sbom-") as td:
        venv_dir = Path(td) / "env"
        _run([sys.executable, "-m", "venv", str(venv_dir)], "creating isolated environment")
        py = _venv_python(venv_dir)

        _run(
            [str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
            "upgrading pip inside the isolated environment",
        )
        # Installing the project WITH its runtime extras makes pip resolve the
        # full closure that actually ships -- the web stack ([web]: fastapi,
        # uvicorn, pymupdf, ...) and the sandbox tooling ([sandbox]: pycdlib) --
        # not just the core. Without the extras the SBOM was missing the entire
        # HTTP stack that serves every request (audit N5).
        _run(
            [str(py), "-m", "pip", "install", "--quiet", f"{project_root}[web,sandbox]"],
            "installing the project and its full runtime closure",
        )

        _run(
            [
                sys.executable,
                "-m",
                "cyclonedx_py",
                "environment",
                str(py),
                "--pyproject",
                str(project_root / "pyproject.toml"),
                "--of",
                "JSON",
                "--sv",
                SPEC_VERSION,
                "-o",
                str(output),
            ],
            f"generating CycloneDX {SPEC_VERSION} SBOM",
        )

    _enrich(output, project_root)


def _canonical(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _parse_requirements_hashes(req_path: Path) -> dict:
    """Map each pinned package name -> its SHA-256 hashes from requirements.txt."""
    import re

    hashes: dict = {}
    current = None
    for line in req_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==", line)
        if m:
            current = _canonical(m.group(1))
            hashes.setdefault(current, [])
        h = re.search(r"--hash=sha256:([0-9a-fA-F]{64})", line)
        if h and current:
            hashes[current].append(h.group(1).lower())
    return hashes


def _sha256_entries(values):
    return [{"alg": "SHA-256", "content": v} for v in values]


def _enrich(output: Path, project_root: Path) -> None:
    """Attach requirements.txt hashes and the pinned non-PyPI runtime components.

    cyclonedx_py's environment scan carries licences but no hashes (audit N5:
    0/20 components carried a digest). The verified SHA-256 for every Python
    package already lives in the hash-pinned requirements.txt; and the runtime
    also ships CPython, kiwix-tools and the WebView2 runtime, whose SHA-256 are
    pinned in cli.PROVISIONING_HASHES. Merge both so the SBOM documents the
    integrity of what actually ships.
    """
    sys.path.insert(0, str(project_root / "src"))
    from knowledge_base_builder.cli import (  # noqa: E402
        PROVISIONING_HASHES, EMBEDDED_PYTHON_VERSION, EMBEDDED_KIWIX_VERSION,
        WEBVIEW2_RUNTIME_VERSION,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    req_hashes = _parse_requirements_hashes(project_root / "requirements.txt")

    # Drop the isolated venv's own build tooling: pip/setuptools/wheel are not
    # part of the product's shipped runtime and carry no pinned digest.
    _TOOLING = {"pip", "setuptools", "wheel"}
    data["components"] = [
        c for c in data.get("components", []) if _canonical(c.get("name", "")) not in _TOOLING
    ]

    # Licences cyclonedx could not extract from installed metadata (declared via
    # classifiers only). A licence is an NTIA minimum element.
    _KNOWN_LICENSES = {"pymupdf": {"id": "AGPL-3.0-or-later"}}

    attached = 0
    for comp in data.get("components", []):
        name = _canonical(comp.get("name", ""))
        digs = req_hashes.get(name)
        if digs and not comp.get("hashes"):
            comp["hashes"] = _sha256_entries(digs)
            attached += 1
        if not comp.get("licenses") and name in _KNOWN_LICENSES:
            comp["licenses"] = [{"license": _KNOWN_LICENSES[name]}]

    # Non-PyPI runtime components the stick boots/serves, with pinned digests.
    H = PROVISIONING_HASHES
    runtime = [
        ("CPython", EMBEDDED_PYTHON_VERSION, "pkg:generic/cpython@" + EMBEDDED_PYTHON_VERSION,
         {"id": "PSF-2.0"},
         [H.get(f"cpython-{EMBEDDED_PYTHON_VERSION}+20250723-x86_64-unknown-linux-gnu-install_only.tar.gz"),
          H.get(f"cpython-{EMBEDDED_PYTHON_VERSION}+20250723-x86_64-apple-darwin-install_only.tar.gz"),
          H.get(f"python-{EMBEDDED_PYTHON_VERSION}-embed-amd64.zip")]),
        ("kiwix-tools", EMBEDDED_KIWIX_VERSION, "pkg:generic/kiwix-tools@" + EMBEDDED_KIWIX_VERSION,
         {"id": "GPL-3.0-or-later"},
         [H.get(f"kiwix-tools_win-x86_64-{EMBEDDED_KIWIX_VERSION}.zip"),
          H.get(f"kiwix-tools_linux-x86_64-{EMBEDDED_KIWIX_VERSION}.tar.gz"),
          H.get(f"kiwix-tools_macos-x86_64-{EMBEDDED_KIWIX_VERSION}.tar.gz")]),
        ("webview2-runtime", WEBVIEW2_RUNTIME_VERSION,
         "pkg:generic/webview2-runtime@" + WEBVIEW2_RUNTIME_VERSION,
         {"name": "Microsoft Software License (WebView2 Runtime, redistributable)"},
         [H.get(f"webview2.runtime.x64.{WEBVIEW2_RUNTIME_VERSION}.nupkg")]),
    ]
    existing = {_canonical(c.get("name", "")) for c in data.get("components", [])}
    for name, version, purl, lic, digests in runtime:
        digs = [d for d in digests if d]
        if not digs or _canonical(name) in existing:
            continue
        data.setdefault("components", []).append({
            "type": "application" if name != "webview2-runtime" else "library",
            "bom-ref": f"{name}@{version}",
            "name": name,
            "version": version,
            "purl": purl,
            "licenses": [{"license": lic}],
            "hashes": _sha256_entries(digs),
        })

    output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    logger.info("enriched: %d PyPI components hashed, %d runtime components added",
                attached, sum(1 for n, *_ in runtime if _canonical(n) not in existing))


def summarise(output: Path) -> None:
    data = json.loads(output.read_text(encoding="utf-8"))
    comps = data.get("components", [])
    subject = data.get("metadata", {}).get("component", {}) or {}
    licensed = sum(1 for c in comps if c.get("licenses"))
    logger.info("SBOM written to %s", output)
    logger.info("  specVersion  : %s", data.get("specVersion"))
    logger.info("  subject      : %s %s", subject.get("name"), subject.get("version"))
    logger.info("  components   : %d", len(comps))
    logger.info("  dependencies : %d", len(data.get("dependencies", [])))
    logger.info("  licensed     : %d/%d", licensed, len(comps))
    if licensed < len(comps):
        unlicensed = [c.get("name") for c in comps if not c.get("licenses")]
        logger.warning("components without a licence: %s", unlicensed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a CycloneDX SBOM.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="destination path (default: <project root>/sbom.json)",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    output = args.output or (project_root / "sbom.json")

    try:
        import cyclonedx_py  # noqa: F401
    except ImportError:
        logger.error("'cyclonedx-bom' is not installed. Run: pip install cyclonedx-bom")
        return 1

    generate(project_root, output)
    summarise(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
