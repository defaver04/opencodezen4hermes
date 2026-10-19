"""Test package for opencodezen4hermes.

The suite is deliberately stdlib-only (BLUEPRINT §8): no pytest, no third-party
imports at test time, so a failure can be reproduced on a bare Python. Keeping
this file a package marker (plus the repo-root path guard below) is what lets
both of these work from the repository root::

    python3 -m unittest discover -s tests -v
    python3 -m unittest tests.test_ids -v
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    # ``discover -s tests`` puts ``tests/`` on sys.path, not the repo root, so
    # ``import sidecar`` would fail when discover picks the start-dir as its
    # top-level directory. Installing the root here fixes that for every module
    # in the package, whichever way the suite was started.
    sys.path.insert(0, _REPO_ROOT)
