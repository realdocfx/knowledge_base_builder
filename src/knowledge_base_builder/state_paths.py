"""One definition of where mutable state lives.

Three modules independently computed ``<root>/.kb_state``: both buckets, the
search index and the audit log. Fixing the buckets alone left the index still
writing into the content root, and the portal still died on a read-only archive
-- the traceback simply moved to a module the first audit had not looked at.

That coupling ties *where the content is* to *where writes go*, which makes the
portal unable to serve from any read-only medium: a write-protected stick, an
optical disc, a read-only network share, or the QEMU sandbox, where the archive
is mounted read-only deliberately because that is the entire safety argument for
handing a VM a physical disk.

Keeping the rule in one place is the point. A second hardcoded ``.kb_state`` is
exactly how this survived its first fix, so ``test_only_one_resolver_defines_
where_state_lives`` fails the build if another appears.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

STATE_DIR_NAME = ".kb_state"
STATE_DIR_ENV = "KBB_STATE_DIR"


def resolve_state_dir(
    root: Union[str, Path],
    state_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """Where mutable state for ``root`` belongs.

    Precedence, and why each rung exists:

    1. ``state_dir`` -- an explicit request wins, so a stray environment variable
       cannot silently redirect a caller that named a location.
    2. ``KBB_STATE_DIR`` -- lets the sandbox redirect every consumer at once
       without threading a path through each construction site.
    3. ``<root>/.kb_state`` -- the historical default, unchanged, so existing
       drives keep their sync state instead of re-downloading everything.
    """
    if state_dir:
        return Path(state_dir)
    env = os.environ.get(STATE_DIR_ENV)
    if env and env.strip():
        return Path(env)
    return Path(root) / STATE_DIR_NAME
