"""The hash-pinned closure must cover every runtime dependency (audit P4).

`_install_portable_packages` installs `--require-hashes -r requirements.txt`, so
any dependency declared in pyproject but missing from requirements.txt simply
never reaches a provisioned stick. That is how pypdf (core), pymupdf ([web]) and
pycdlib ([sandbox]) went missing — shipping sticks without server-side PDF
rendering or the non-ZIM media ISO, silently. Bind the two so it can't recur.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

_ROOT = Path(__file__).resolve().parents[1]

# Documented exceptions: not resolvable from PyPI, so deliberately not in the
# hash-pinned PyPI closure.
_NOT_FROM_PYPI = {"xapian-bindings"}


def _names(deps):
    out = set()
    for d in deps:
        name = re.split(r"[<>=!;\[ ]", d, maxsplit=1)[0].strip().lower().replace("_", "-")
        if name:
            out.add(name)
    return out


def _declared_runtime_deps():
    proj = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    required = _names(proj.get("dependencies", []))
    extras = proj.get("optional-dependencies", {})
    for extra in ("web", "sandbox"):  # runtime extras a provisioned stick uses
        required |= _names(extras.get(extra, []))
    return required - _NOT_FROM_PYPI


def _pinned_in_requirements():
    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    # Names may carry extras, e.g. `internetarchive[speedups]==` / `uvicorn[standard]==`.
    pinned = re.findall(r"(?m)^([a-z0-9._-]+)(?:\[[^\]]*\])?==", text)
    return {name.replace("_", "-") for name in pinned}


def test_requirements_pins_every_declared_runtime_dependency():
    missing = _declared_runtime_deps() - _pinned_in_requirements()
    assert not missing, (
        f"requirements.txt omits declared runtime dependencies {sorted(missing)} "
        "(audit P4) -- regenerate with "
        "`pip-compile --generate-hashes requirements.in`"
    )


def test_the_recovered_three_are_present_with_hashes():
    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    for pkg in ("pypdf", "pymupdf", "pycdlib"):
        block = re.search(rf"(?mi)^{pkg}==\S+(?:.*\n)+?(?=^\S|\Z)", text)
        assert block, f"{pkg} not pinned in requirements.txt"
        assert "--hash=" in block.group(0), f"{pkg} pinned without a hash"
