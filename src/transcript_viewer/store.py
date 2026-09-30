"""Small JSON files under ~/.transcript-viewer, written so a crash cannot truncate them.

The library and the settings both keep a single JSON document that must survive
an interrupted write — losing every annotation, or a stored credential, because
a process died mid-`write()` is not an acceptable failure. Both go through here.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path.home() / ".transcript-viewer"

# What the tool was called before, and where it kept things then. A rename
# should not look like data loss, so a library left by the old name is adopted
# once, on the first run that finds one.
LEGACY_ROOT = Path.home() / ".atif"


def adopt_legacy() -> bool:
    """Take over a library the tool left under its former name.

    Returns whether anything moved. Absolute paths recorded inside the index
    point into the old directory, so they are rewritten as part of the move —
    a copy left behind would be worse than not moving at all.
    """
    if ROOT.exists() or not LEGACY_ROOT.exists():
        return False

    LEGACY_ROOT.rename(ROOT)  # same filesystem, so this is atomic

    index = ROOT / "index.json"
    if index.exists():
        text = index.read_text()
        moved = text.replace(str(LEGACY_ROOT), str(ROOT))
        if moved != text:
            index.write_text(moved)
    return True


def read_json(path: Path) -> dict[str, Any]:
    """A JSON object, or {} if the file is missing or damaged.

    Refusing to start because one byte is wrong is worse than starting empty,
    so a corrupt file is treated as absent rather than raised.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: dict[str, Any], private: bool = False) -> None:
    """Write atomically: temp file in the same directory, then os.replace.

    Same directory so the replace is a rename within one filesystem, which is
    atomic; a temp file elsewhere would degrade to a copy. `private` marks a
    file only the owner may read — for anything holding a credential.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        os.chmod(path.parent, 0o700)

    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp", delete=False
    )
    try:
        with handle as out:
            out.write(json.dumps(payload, indent=2))
            out.flush()
            os.fsync(out.fileno())
        # mkstemp already creates at 0600; set it anyway so the guarantee is
        # stated here rather than inherited from a library's implementation.
        os.chmod(handle.name, 0o600 if private else 0o644)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
