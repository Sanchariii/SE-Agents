"""Reject a generated program before it runs.

Extends the deny-list in `main_v3::_static_code_check` with the brand rules this design
needs. Three of the four new checks exist because a program that does these things has
escaped the design's guarantees entirely: constructing its own `Presentation` abandons
the template, calling `shapes.add_*` abandons the pre-styled component API, and a hex
literal abandons the theme.

A rejection is returned to the model in the same repair loop as a traceback or a
geometry violation, so the wording matters: each problem says what to do instead.
"""

import ast
import re
from typing import Optional

from app.utilities.logger_util import get_logger

log = get_logger(__name__)

FORBIDDEN_MODULES = frozenset({
    "subprocess", "socket", "shutil", "urllib", "requests", "http", "ftplib",
    "ctypes", "importlib", "multiprocessing",
})
FORBIDDEN_CALLS = frozenset({
    "os.system", "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.popen",
    "eval", "exec", "__import__", "os.execv", "os.spawn",
})

#: The module the sandbox copies in beside the program. It is the only way to a deck.
BUILDER_MODULE = "deck_builder"

#: Closing calls the model invents instead of `deck.save(OUTPUT_PATH)`. `DeckBuilder`'s
#: whole public surface is add_slide / resolve_asset / save / warnings, so every one of
#: these is an AttributeError at the very last line - after the slide has been fully
#: built, which is the most expensive possible moment to fail. In the analysed
#: production log `deck.finalize()` alone accounted for three of the four program
#: crashes. Caught here, the repair costs no execution at all.
INVENTED_SAVE_METHODS = frozenset({
    "finalize", "finalise", "finish", "render", "export", "write", "close",
    "save_deck", "save_presentation", "build",
})

_HASH_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_BARE_HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")
_FENCE_RE = re.compile(r"^```(?:python|py)?\s*\n(.*?)\n?```\s*$", re.DOTALL)

#: Keyword arguments whose value is a colour. A bare six-character string is only a
#: hex literal when it arrives through one of these; anywhere else it is content.
#: "100000 units shipped" and part number "A1B2C3" are both six hex characters, and
#: rejecting them tells the model to replace its own data with a brand token, which it
#: cannot do - so the slide spends its whole repair budget and falls back.
_COLOUR_KEYWORDS = ("color", "colour", "fill", "accent")


def strip_code_fences(code: str) -> str:
    """Unwrap a ```python fenced block, which the model sometimes answers with.

    Args:
        code: The model's answer, fenced or not.

    Returns:
        The program source, without the fence.
    """
    match = _FENCE_RE.match((code or "").strip())
    if match:
        log.debug("stripped a code fence: %d chars in, %d out",
                  len(code or ""), len(match.group(1)))
        return match.group(1)
    return (code or "").strip()


def check(code: str) -> list[str]:
    """Everything wrong with this program that can be seen without running it.

    Args:
        code: The generated program's source.

    Returns:
        One instruction per problem, empty when the program is acceptable.
    """
    log.debug("static-checking a %d-char program", len(code or ""))
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        log.info("generated program does not parse: %s at line %s", exc.msg, exc.lineno)
        return [f"SyntaxError: {exc.msg} (line {exc.lineno}) - fix the syntax and "
                f"resend the whole program"]

    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([alias.name for alias in node.names]
                     if isinstance(node, ast.Import) else [node.module or ""])
            for name in names:
                if name.split(".")[0] in FORBIDDEN_MODULES:
                    problems.append(
                        f"import of '{name}' is not allowed; a slide program needs "
                        f"nothing but `{BUILDER_MODULE}`")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            problems.extend(_assign_problems(node))
        elif isinstance(node, ast.Call):
            problems.extend(_call_problems(node))
            problems.extend(_colour_argument_problems(node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _HASH_HEX_RE.match(node.value.strip()):
                problems.append(_hex_problem(node.value))

    if (f"from {BUILDER_MODULE} import" not in code
            and f"import {BUILDER_MODULE}" not in code):
        problems.append(
            f"the program must build the slide through the DeckBuilder API: start with "
            f"`from {BUILDER_MODULE} import DeckBuilder`")

    # Preserve order, drop repeats: the same mistake on twelve lines is one instruction.
    unique = list(dict.fromkeys(problems))
    if unique:
        log.info("static guard rejected the program: %d problem(s) (%d before "
                 "de-duplication): %s", len(unique), len(problems),
                 " | ".join(p[:80] for p in unique))
    else:
        log.debug("static guard accepted the program")
    return unique


def _hex_problem(value: str) -> str:
    """The instruction a hex colour literal earns."""
    return (f"the hex colour literal '{value}' is not allowed; use a brand token name "
            f"such as 'red', 'navy' or 'accent1' - every colour must resolve against "
            f"the template theme")


def _colour_argument_problems(node: ast.Call) -> list[str]:
    """Bare six-hex strings passed where a colour is expected.

    A `#`-prefixed value is unambiguous and is caught wherever it appears. A bare one
    is only a colour when it arrives as a colour argument; see `_COLOUR_KEYWORDS`.

    Args:
        node: The call being inspected.

    Returns:
        One instruction per offending argument.
    """
    problems: list[str] = []
    for keyword in node.keywords:
        name = (keyword.arg or "").lower()
        if not any(marker in name for marker in _COLOUR_KEYWORDS):
            continue
        value = keyword.value
        if (isinstance(value, ast.Constant) and isinstance(value.value, str)
                and _BARE_HEX_RE.match(value.value.strip())):
            log.debug("bare hex %r passed as %s= on line %s", value.value, name,
                      getattr(node, "lineno", "?"))
            problems.append(_hex_problem(value.value))
    return problems


def _assign_problems(node) -> list[str]:
    """Problems with one assignment.

    The only one so far is renaming a shape. Shape names are how the geometry gate
    tells the brand furniture it stamped from the program's own output, so a program
    that sets `.name` could exempt itself from the rules protecting the footer band.

    Args:
        node: An `ast.Assign` or `ast.AnnAssign`.

    Returns:
        One instruction per problem with this assignment.
    """
    targets = (list(node.targets) if isinstance(node, ast.Assign) else [node.target])
    if any(isinstance(t, ast.Attribute) and t.attr == "name" for t in targets):
        log.warning("generated program tried to rename a shape on line %s; that is how "
                    "the gate identifies brand furniture", getattr(node, "lineno", "?"))
        return ["do not set a shape's `.name`; shape names are how the geometry gate "
                "tells the brand furniture from your own shapes, and renaming one "
                "does not change where it sits"]
    return []


def _call_problems(node: ast.Call) -> list[str]:
    """Problems with one call node.

    Args:
        node: The call being inspected.

    Returns:
        One instruction per problem with this call.
    """
    func = node.func
    problems: list[str] = []

    if isinstance(func, ast.Name):
        if func.id == "Presentation":
            problems.append(
                "do not construct `Presentation(...)` - `DeckBuilder` opens the brand "
                "template for you, and a deck built any other way inherits no theme, "
                "no fonts and no masters")
        elif func.id == "RGBColor":
            problems.append(
                "do not use `RGBColor(...)` - pass a brand token name instead, so the "
                "colour comes from the template theme")
        elif func.id in FORBIDDEN_CALLS:
            problems.append(f"call to '{func.id}' is not allowed; remove it")

    if isinstance(func, ast.Attribute):
        if func.attr.startswith("add_") and _is_shapes_access(func.value):
            problems.append(
                f"do not call `.shapes.{func.attr}(...)` directly; use the DeckBuilder "
                f"component API (add_card, add_stat, add_shape, add_table, add_chart, "
                f"add_textbox, add_line, add_image, add_icon_circle) so the result is "
                f"styled and measurable")
        if func.attr in INVENTED_SAVE_METHODS:
            problems.append(
                f"`DeckBuilder` has no `{func.attr}()` method. There is exactly one "
                f"way to finish a slide program: `deck.save(OUTPUT_PATH)`. Replace "
                f"the `{func.attr}()` call with it")
        dotted = f"{_dotted_name(func.value)}.{func.attr}" if _dotted_name(func.value) else ""
        if dotted and (dotted in FORBIDDEN_CALLS
                       or any(dotted.startswith(name) for name in FORBIDDEN_CALLS
                              if name.endswith("spawn"))):
            problems.append(f"call to '{dotted}' is not allowed; remove it")
    return problems


def _dotted_name(node: ast.expr) -> str:
    """`os.path` for an attribute chain of plain names, "" for anything else."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ""
    parts.append(current.id)
    return ".".join(reversed(parts))


def _is_shapes_access(node: ast.expr) -> bool:
    """True for `<anything>.shapes` - the attribute the component API replaces."""
    return isinstance(node, ast.Attribute) and node.attr == "shapes"


def check_message(code: str) -> Optional[str]:
    """All problems as one message for the repair loop, or None when clean.

    Args:
        code: The generated program's source.

    Returns:
        The rejection the model receives, or None when the program may run.
    """
    problems = check(code)
    if not problems:
        return None
    message = ("The program was rejected before running. Fix every point and resend "
               "the whole program:\n" + "\n".join(f"- {p}" for p in problems))
    log.debug("returning a %d-char rejection to the repair loop", len(message))
    return message
