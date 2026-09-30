"""Run the browser-script tests as part of the normal suite.

The viewer is one HTML page, so most of its behaviour is JavaScript that pytest
cannot reach. Two real breaks shipped that way — a row click that did nothing
and a trajectory pane stuck on "Converting…" — so the checks live in
tests/page.test.js and run from here.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SUITE = Path(__file__).parent / "page.test.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_script_behaves():
    result = subprocess.run(
        ["node", str(SUITE)], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        pytest.fail(result.stdout + result.stderr)
