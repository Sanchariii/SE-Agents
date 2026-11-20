"""Import hygiene.

`scripts/mainv3/` was reference material, ported from and then deleted. Nothing under
`app/` ever imported it and nothing ever may: an application that reaches into
`scripts/` cannot be deployed without shipping the scripts too.

The second test is the one that keeps the sealed sandbox sealed.
"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", sorted(APP.rglob("*.py")), ids=lambda p: p.name)
def test_no_module_under_app_imports_from_scripts(path):
    offenders = [name for name in _imports(path)
                 if name == "scripts" or name.startswith("scripts.")]
    assert not offenders, f"{path} imports {offenders} from scripts/"


def test_the_deck_builder_imports_nothing_from_the_app():
    """It is copied into the sandbox, where `app` does not exist. One app import here
    breaks every generated slide, and only at runtime."""
    path = APP / "genai" / "ppt" / "deck_builder.py"
    offenders = [name for name in _imports(path)
                 if name == "app" or name.startswith("app.")]
    assert not offenders, f"deck_builder.py imports {offenders}; it must stand alone"


@pytest.mark.parametrize("retired", [
    "app.genai.ppt.renderer",
    "app.genai.ppt.audit",
    "app.genai.ppt.template.filler",
    "app.genai.ppt.template.icons",
])
def test_retired_modules_are_gone_and_unreferenced(retired):
    for path in APP.rglob("*.py"):
        assert retired not in _imports(path), f"{path} still imports {retired}"
    with pytest.raises(ModuleNotFoundError):
        __import__(retired)


def test_the_potx_glob_is_gone():
    """Decision 2: a single constant resolves the template. A glob could resolve it to
    one of the five .potx files still on disk, silently."""
    from app.genai.ppt.template import registry

    assert not hasattr(registry, "available")
    assert not hasattr(registry, "path_for")
    source = Path(registry.__file__).read_text(encoding="utf-8")
    assert ".potx" not in source


def test_the_retired_builder_and_fallback_entry_points_are_gone():
    """`build_pptx` rendered onto a .potx and `plain_slide_pptx` onto a blank layout;
    both are replaced by DeckBuilder, and a caller finding one of them again would be
    building decks outside the gate entirely."""
    from app.genai.agents import ppt_creator_agent
    from app.genai.ppt import builder

    assert not hasattr(builder, "build_pptx")
    assert hasattr(builder, "persist_deck")          # deck_workflow imports this
    assert not hasattr(ppt_creator_agent, "plain_slide_pptx")
