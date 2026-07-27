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
        # Installing the project makes pip resolve exactly the runtime closure
        # declared in pyproject -- nothing more, nothing less.
        _run(
            [str(py), "-m", "pip", "install", "--quiet", str(project_root)],
            "installing the project and its runtime dependencies",
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
