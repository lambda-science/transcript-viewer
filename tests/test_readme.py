"""Keep the short README accurate without forcing implementation detail into it."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text()


README = _read("README.md")
# Wrapped prose: a claim can straddle a line break, so compare on one line.
FLAT = " ".join(README.split())


# (claim, marker proving it in the source, source file)
CLAIMS = [
    ("~/.transcript-viewer/config.json", 'store.ROOT / "config.json"', "src/transcript_viewer/config.py"),
    ("0600", "0o600", "src/transcript_viewer/store.py"),
    ("0700", "0o700", "src/transcript_viewer/store.py"),
    ("nosniff", "X-Content-Type-Options", "src/transcript_viewer/viewer.py"),
    ("re-read whenever it changes", "def page()", "src/transcript_viewer/viewer.py"),
    ("stat per page load", "PAGE_PATH.stat()", "src/transcript_viewer/viewer.py"),
    (
        "uvx --from git+https://github.com/lambda-science/transcript-viewer.git transcript-viewer",
        "transcript-viewer = ",
        "pyproject.toml",
    ),
    ("binds `127.0.0.1`", '"127.0.0.1"', "src/transcript_viewer/viewer.py"),
    ("take what is new", "def cmd_sync", "src/transcript_viewer/cli.py"),
    ("only because it is named in", "identity_header", "src/transcript_viewer/deploy.py"),
]


@pytest.mark.parametrize(
    ("claim", "marker", "source"), CLAIMS, ids=[c[0] for c in CLAIMS]
)
def test_a_documented_behaviour_still_exists(claim, marker, source):
    assert claim in README, f"the README no longer makes this claim: {claim!r}"
    assert marker in _read(source), (
        f"the README claims {claim!r} but {source} no longer contains {marker!r}"
    )


# The library changed shape more than anything else this session, and the
# checks above missed all of it: three claims about collections, an index
# command and a removed instruction all survived a full pass.
LIBRARY = [
    ("Local", "Local/", "src/transcript_viewer/viewer.py"),
    ("Remote", '"Remote"', "src/transcript_viewer/viewer.py"),
    ("derived", "def _group", "src/transcript_viewer/viewer.py"),
    ("Add…", "function openAdd", "src/transcript_viewer/page.html"),
    ("picker", "function drawTree", "src/transcript_viewer/page.html"),
    ("measured", "def measure", "src/transcript_viewer/fetch.py"),
    ("one level at a time", "def browse", "src/transcript_viewer/fetch.py"),
    ("aws", "def _aws", "src/transcript_viewer/fetch.py"),
    ("Clear library", "function clearLibrary", "src/transcript_viewer/page.html"),
]


@pytest.mark.parametrize(("claim", "marker", "source"), LIBRARY, ids=[c[0] for c in LIBRARY])
def test_the_library_is_described_as_it_works(claim, marker, source):
    assert claim in FLAT, f"the README no longer describes: {claim!r}"
    assert marker in _read(source), (
        f"the README claims {claim!r} but {source} no longer contains {marker!r}"
    )


# Features that went. The README should not read as though they are still here.
RETIRED = [
    ("atif-make index", "a command that no longer exists"),
    ("File into…", "filing by hand, replaced by the derived tree"),
    ("Keep watching", "the timer, replaced by a refresh"),
    ("filed into a collection", "collections, replaced by the tree"),
    ("editable in the dialog", "the download destination field"),
]


@pytest.mark.parametrize(("wording", "why"), RETIRED, ids=[r[0] for r in RETIRED])
def test_a_removed_feature_is_not_still_offered(wording, why):
    assert wording not in FLAT, f"still described though gone ({why}): {wording!r}"


def test_the_first_run_is_described_as_empty():
    """It scanned a whole machine unasked before; the README said so too."""
    assert "opens empty" in README or "empty on a first run" in README
    assert "corpus.load() or corpus.scan()" not in _read("src/transcript_viewer/cli.py")


def test_the_page_is_a_file_of_its_own():
    assert (ROOT / "src/transcript_viewer/page.html").exists()
    viewer = _read("src/transcript_viewer/viewer.py")
    assert 'PAGE = r"""' not in viewer, "the page is back inside the module"
    assert "page.html" in viewer


# Behaviour that was removed, and the wording that described it.
REMOVED = [
    ("disable_nagle_algorithm", "src/transcript_viewer/viewer.py", "TCP_NODELAY is set"),
    ("summary · new", "src/transcript_viewer/page.html", "summary · new"),
]


@pytest.mark.parametrize(
    ("marker", "source", "wording"), REMOVED, ids=[r[0] for r in REMOVED]
)
def test_removed_behaviour_is_not_still_documented(marker, source, wording):
    if marker in _read(source):
        pytest.skip(f"{marker} is back in the code; this guard no longer applies")
    assert wording not in README, (
        f"{marker!r} was removed from {source} but the README still describes it"
    )


def test_the_readme_recommends_uv_rather_than_pip():
    """Installing this with pip is not how it is meant to be used."""
    assert "pip install" not in README


def test_every_module_appears_in_the_map():
    """A file nobody documented is a file nobody knows to look at."""
    readme = Path("README.md").read_text()
    for path in sorted(Path("src/transcript_viewer").iterdir()):
        if path.name.startswith("_") or path.suffix not in {".py", ".html"}:
            continue
        assert f"  {path.name}" in readme, f"{path.name} is missing from the module map"


def test_every_extra_offered_in_the_readme_exists():
    """An install command that names an extra nobody defined simply fails."""
    project = Path("pyproject.toml").read_text()
    defined = set(re.findall(r"^([a-z][a-z0-9-]*) = \[", project, re.MULTILINE))
    readme = Path("README.md").read_text()
    offered = set()
    for group in re.findall(r'transcript-viewer\[([a-z,]+)\]', readme):
        offered.update(group.split(","))
    missing = offered - defined
    assert not missing, f"the README offers extras that do not exist: {missing}"


def test_every_extra_that_exists_is_mentioned():
    """An extra nobody is told about may as well not be there."""
    project = Path("pyproject.toml").read_text()
    _, found, rest = project.partition("[project.optional-dependencies]")
    block = rest.split("\n[", 1)[0] if found else ""
    defined = set(re.findall(r"^([a-z][a-z0-9-]*) = \[", block, re.MULTILINE))
    readme = Path("README.md").read_text()
    unmentioned = {name for name in defined if name not in readme}
    assert not unmentioned, f"extras the README never mentions: {unmentioned}"
