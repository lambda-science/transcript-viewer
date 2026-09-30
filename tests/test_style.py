"""The page's stylesheet, checked for the mistakes CSS makes silently.

A broken selector is not an error: the browser drops the rule and renders on.
Both faults these guard against shipped unnoticed — a header button styled as
bare text beside two pills, and a line whose newlines were the two characters
`\\n`, which killed the two rules after it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import pytest

PAGE = (ROOT / "src/transcript_viewer/page.html").read_text()


def _rules(css: str) -> str:
    """One rule per line, no spacing, comments gone.

    The page is auto-formatted, so the same rule may arrive on one line or on
    six with a space after every colon. Not one test here is about that, and
    every one of them was written against the compact form — so normalise once
    on the way in rather than teaching each regex to expect either.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = re.sub(r"\s*([{};:,])\s*", r"\1", css)
    return re.sub(r"\}", "}\n", css)


STYLE = _rules(PAGE[PAGE.index("<style>") + 7 : PAGE.index("</style>")])


HEADER_CONTROLS = ("#theme", "#gear", "#open")


def test_the_header_controls_share_one_rule():
    """Separate copies of a box is how the settings button came to look different."""
    shared = re.search(r"^(#[\w,#-]+)\{[^}]*border-radius:8px", STYLE, re.MULTILINE)
    assert shared, "no shared rule defines the header control box"
    named = set(shared.group(1).split(","))
    missing = [c for c in HEADER_CONTROLS if c not in named]
    assert not missing, f"not in the shared rule, so they will drift: {missing}"


@pytest.mark.parametrize("control", HEADER_CONTROLS)
def test_no_header_control_redefines_its_own_box(control):
    """A later rule of its own is exactly how one drifts away from the others."""
    own = re.findall(rf"^{re.escape(control)}\{{([^}}]*)\}}", STYLE, re.MULTILINE)
    for body in own:
        for prop in ("background", "border", "border-radius", "padding"):
            assert prop not in body, (
                f"{control} sets {prop} on its own; it belongs in the shared rule"
            )


def test_no_rule_is_killed_by_an_escaped_newline():
    r"""`\n` in a stylesheet is an escaped letter n, not a line break.

    The parser then treats what follows as part of a selector, drops the rule as
    invalid, and says nothing.
    """
    assert "\\n" not in STYLE, "a literal backslash-n is in the stylesheet"


def test_no_selector_sets_the_same_property_twice():
    """A second rule that adds properties is ordinary CSS; one that overrides the
    same property leaves a dead value behind, which is how a stale 320px sidebar
    width outlived the design that used it."""
    dead = []
    for name in set(re.findall(r"^([#.][\w-]+(?: [\w.#-]+)?)\{", STYLE, re.MULTILINE)):
        props: dict[str, int] = {}
        for body in re.findall(rf"^{re.escape(name)}\{{([^}}]*)\}}", STYLE, re.MULTILINE):
            for declaration in body.split(";"):
                if ":" not in declaration:
                    continue
                prop = declaration.split(":", 1)[0].strip()
                props[prop] = props.get(prop, 0) + 1
        dead += [f"{name} {{{p}}}" for p, count in props.items() if count > 1]
    assert not dead, f"set more than once, so an earlier value is dead: {sorted(dead)}"


def test_every_rule_closes():
    assert STYLE.count("{") == STYLE.count("}"), "an unbalanced brace in the stylesheet"


def test_colours_outside_the_themes_come_from_tokens():
    """Literal colours belong in the :root blocks that define a theme. Anywhere
    else they ignore whichever theme the reader chose."""
    without_themes = re.sub(r":root[^{]*\{[^}]*\}", "", STYLE)
    literals = re.findall(r":\s*(#[0-9a-fA-F]{3,8})\b", without_themes)
    assert not literals, f"hard-coded colours defeat the themes: {sorted(set(literals))}"


def test_a_checkbox_is_not_styled_as_a_text_field():
    """`.sheet input {width:100%}` caught checkboxes and swallowed whole rows."""
    for match in re.finditer(r"^\.sheet input([^{]*)\{([^}]*)\}", STYLE, re.MULTILINE):
        qualifier, body = match.group(1), match.group(2)
        if "checkbox" in qualifier:
            continue
        assert "not([type=checkbox])" in qualifier or "width:100%" not in body, (
            f".sheet input{qualifier} sets width on every input, checkboxes included"
        )


def test_the_picker_rows_lay_out_left_to_right():
    assert re.search(r"^\.pkrow\{[^}]*display:flex", STYLE, re.MULTILINE)
    assert re.search(r"^\.pkname\{[^}]*flex:1", STYLE, re.MULTILINE)


def test_no_template_expression_is_left_in_the_static_markup():
    """The modal's HTML is not a template literal: a ${…} there renders as text,
    which is how the examples row shipped showing its own source."""
    markup = PAGE[: PAGE.index("<script>")]
    assert "${" not in markup, "a JS template expression is sitting in static HTML"


def test_sizes_go_through_one_formatter():
    """Every place used to divide by 1048576 and say MB, so a 900-byte log read
    as "0.0 MB" and a 144 GB bucket as "147456.0 MB"."""
    script = PAGE[PAGE.index("<script>") : PAGE.rindex("</script>")]
    code = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith(("/*", "*", "//"))
    )
    assert "1048576" not in code, "a size is still being divided by hand"
    assert code.count("function bytes(") == 1


# Numbers a reader sees are formatted in one place, so a corpus of thousands
# reads as "2,258 sessions" rather than "2258 sessions". These are the shapes
# that reached the screen raw before anyone looked.
RAW_NUMBERS = (
    (r"\.length\s*\+\s*[\"']", "a count concatenated onto a string"),
    (r"\$\{[^}]*\.length\}\s*(?:session|step|file|result|match)", "${…length} beside a noun"),
    (r"\$\{\s*[a-zA-Z_$][\w.$]*\.count\s*\}", "${…count}"),
    (r"\$\{\s*[a-zA-Z_$][\w.$]*\.added\s*\}", "${…added}"),
    (r"\$\{\s*s\.step_id\s*\}", "${s.step_id}"),
)


@pytest.mark.parametrize(("pattern", "what"), RAW_NUMBERS, ids=[w for _, w in RAW_NUMBERS])
def test_a_number_on_screen_goes_through_the_formatter(pattern, what):
    script = PAGE[PAGE.index("<script>") : PAGE.rindex("</script>")]
    hits = []
    for i, line in enumerate(script.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        for match in re.finditer(pattern, line):
            around = line[max(0, match.start() - 14) : match.end() + 3]
            if "num(" in around or "bytes(" in around:
                continue
            hits.append(f"line {i}: {stripped[:90]}")
    assert not hits, f"{what} without num(): " + "; ".join(hits)


def test_the_session_count_is_said_in_one_place():
    """It was written out three times, and one of them was already wrong."""
    script = PAGE[PAGE.index("<script>") : PAGE.rindex("</script>")]
    assert re.search(r"const showCount\s*=", script), (
        "the count should have a single home"
    )
    assert len(re.findall(r"count\.textContent\s*=", script)) <= 2, (
        "the session count is being written in more than one place again"
    )


def _rule(selector: str) -> str:
    """The declarations of one rule, with the spacing taken out.

    The page is auto-formatted, so a rule may be written on one line or on six
    with spaces around every colon. Every test here is about what a rule says,
    and none of them is about that, so the whitespace goes before they see it.
    """
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", STYLE)
    assert match, f"no rule for {selector}"
    return re.sub(r"\s+", "", match.group(1))


def test_a_command_block_keeps_its_first_line_inside_itself():
    """A negative text-indent pulls the first line out of the box.

    `pre` carries overflow:auto, so the box then scrolls sideways to chase it:
    the first word of every command disappeared and the prompt mark was left
    stranded mid-block. Both symptoms, one declaration.
    """
    rule = _rule(".sh")
    assert "text-indent:-" not in rule.replace(" ", ""), (
        "a negative text-indent on .sh hides the start of the command"
    )


def test_a_command_wraps_rather_than_scrolling():
    rule = _rule(".sh").replace(" ", "").replace("\n", "")
    assert "white-space:pre-wrap" in rule, "a command that runs off the side is not read"
    assert "overflow-x:auto" not in rule, "it should wrap, not scroll"


def test_the_prompt_mark_stays_at_the_top_left():
    rule = _rule(".sh::before").replace(" ", "").replace("\n", "")
    assert "position:absolute" in rule
    assert "left:11px" in rule and "top:10px" in rule


def _theme_blocks() -> dict[str, set[str]]:
    """The tokens each `data-theme` block defines, by theme name.

    One block carries two selectors — `:root` and `:root[data-theme="paper"]` —
    so the name is read off the attribute rather than off the whole selector.
    """
    palettes: dict[str, set[str]] = {}
    for selector, body in re.findall(
        r"^([^{\n]*\[data-theme=[^{]*)\{([^}]*)\}", STYLE, re.MULTILINE
    ):
        tokens = {d.split(":", 1)[0] for d in body.split(";") if d.startswith("--")}
        for name in re.findall(r'\[data-theme="([\w-]+)"\]', selector):
            palettes[name] = tokens
    return palettes


def test_every_theme_defines_the_whole_palette():
    """A theme that leaves a token out is not an error anyone sees.

    It inherits that one from `:root`, which is paper — so a cool sidebar keeps
    a warm cream line through it and nothing says why.
    """
    palettes = _theme_blocks()
    assert len(palettes) >= 3, "the theme blocks are no longer where this looks"
    whole = set().union(*palettes.values())
    short = {name: sorted(whole - t) for name, t in palettes.items() if whole - t}
    assert not short, f"themes silently inheriting paper's value: {short}"


def test_the_switcher_offers_every_theme_the_stylesheet_defines():
    """A palette nothing cycles to is a palette nobody sees."""
    script = PAGE[PAGE.index("<script>") : PAGE.rindex("</script>")]
    cycled = re.search(r"const THEMES\s*=\s*\[([^\]]*)\]", script)
    assert cycled, "the theme cycle is no longer a literal list"
    named = set(re.findall(r'"([\w-]+)"', cycled.group(1)))
    assert named == set(_theme_blocks()), (
        f"the cycle and the stylesheet disagree: {named ^ set(_theme_blocks())}"
    )
    labels = re.search(r"const THEME_LABEL\s*=\s*\{([^}]*)\}", script)
    assert labels, "the button has no labels to show"
    for name in named:
        assert re.search(rf"\b{re.escape(name)}\s*:", labels.group(1)), (
            f"{name} would put its own id on the button"
        )
