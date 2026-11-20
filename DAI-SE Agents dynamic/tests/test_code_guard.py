"""The static guard. A rejection here is a repair round the sandbox never has to pay for."""

import pytest

from app.genai.ppt import code_guard


CLEAN = '''
from deck_builder import DeckBuilder

deck = DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)
slide = deck.add_slide("M1-L10")
slide.set_title("Reflow profile control")
slide.add_card("Yield", ["Up 4 points"], *slide.grid(0, 1, cols=4, rows=2))
slide.add_stat("98%", "first-pass yield", *slide.grid(5, 1, cols=3, rows=2))
slide.set_notes("why it matters")
deck.save(OUTPUT_PATH)
'''


def test_a_representative_deckbuilder_program_is_accepted():
    assert code_guard.check(CLEAN) == []
    assert code_guard.check_message(CLEAN) is None


def test_a_syntax_error_is_reported_with_its_line():
    problems = code_guard.check("def broken(:\n    pass\n")
    assert len(problems) == 1 and "SyntaxError" in problems[0] and "line" in problems[0]


def test_constructing_a_presentation_directly_is_refused():
    code = CLEAN + "\nfrom pptx import Presentation\nprs = Presentation()\n"
    assert any("Presentation(" in p for p in code_guard.check(code))


def test_calling_shapes_add_directly_is_refused():
    code = CLEAN + "\nslide.slide.shapes.add_textbox(1, 2, 3, 4)\n"
    problems = code_guard.check(code)
    assert any("add_textbox" in p and "component API" in p for p in problems)


@pytest.mark.parametrize("literal", ['"#CD163F"', '"CD163F"', "'#003e7e'"])
def test_a_hex_colour_literal_is_refused(literal):
    code = CLEAN + f"\nslide.add_shape('rectangle', 1, 1, 1, 1, fill={literal})\n"
    assert any("hex" in p.lower() for p in code_guard.check(code))


def test_rgbcolor_is_refused():
    code = CLEAN + "\nfrom pptx.dml.color import RGBColor\nc = RGBColor(1, 2, 3)\n"
    assert any("RGBColor" in p for p in code_guard.check(code))


@pytest.mark.parametrize("module", ["subprocess", "socket", "urllib", "requests",
                                    "shutil", "ctypes", "importlib", "multiprocessing"])
def test_forbidden_modules_are_refused(module):
    code = CLEAN + f"\nimport {module}\n"
    assert any(module in p and "not allowed" in p for p in code_guard.check(code))


@pytest.mark.parametrize("call", ["os.system('ls')", "eval('1')", "exec('x=1')",
                                  "os.remove('f')", "__import__('os')"])
def test_forbidden_calls_are_refused(call):
    assert any("not allowed" in p for p in code_guard.check(CLEAN + f"\n{call}\n"))


def test_a_program_that_never_imports_the_builder_is_refused():
    assert any("deck_builder" in p for p in code_guard.check("x = 1\n"))


def test_code_fences_are_stripped_before_checking():
    fenced = "```python\n" + CLEAN + "```"
    assert code_guard.check(code_guard.strip_code_fences(fenced)) == []


def test_every_problem_reads_as_an_instruction_not_an_assertion():
    problems = code_guard.check("import socket\n")
    assert problems
    for problem in problems:
        assert problem[0].islower() or problem.startswith(("SyntaxError", "import"))
        assert len(problem) > 20, "a violation must tell the model what to do instead"


def test_the_same_mistake_on_many_lines_is_one_instruction():
    """Twelve hex literals must not produce twelve identical repair instructions."""
    code = CLEAN + "\n" + "\n".join(
        f"slide.add_shape('rectangle', {i}, 1, 1, 1, fill='CD163F')" for i in range(6))
    hexes = [p for p in code_guard.check(code) if "hex" in p.lower()]
    assert len(hexes) == 1


def test_renaming_a_shape_is_refused():
    """Shape names are how the gate tells brand furniture from the program's output;
    a program that could rename a shape could exempt itself from rules 1 and 5."""
    code = CLEAN + "\nshape = slide.add_textbox('x', 1, 7.1, 2, 0.3)\nshape.name = 'frame:logo'\n"
    assert any(".name" in p for p in code_guard.check(code))


def test_content_that_happens_to_be_six_hex_characters_is_not_a_colour():
    """"100000 units shipped" and part number "A1B2C3" are both six hex characters.
    Rejecting them tells the model to replace its own data with a brand token, which
    it cannot do, so the slide spends its whole repair budget and falls back."""
    code = CLEAN + (
        "\nslide.add_stat('100000', 'units shipped', 1, 1, 3, 2)"
        "\nslide.set_bullets('body1', ['Part A1B2C3 reworked twice'])\n")
    assert code_guard.check(code) == []


def test_a_hash_prefixed_hex_is_refused_wherever_it_appears():
    """With the hash there is no ambiguity, so it does not need to be in a colour
    argument to be wrong."""
    code = CLEAN + "\naccent = '#CD163F'\n"
    assert any("hex" in p.lower() for p in code_guard.check(code))


@pytest.mark.parametrize("call", [
    "slide.add_card('T', ['b'], 1, 1, 3, 2, accent='CD163F')",
    "slide.add_stat('9', 'l', 1, 1, 3, 2, color='003E7E')",
    "slide.add_textbox('x', 1, 1, 3, 1, fill='F58024')",
])
def test_a_bare_hex_in_a_colour_argument_is_still_refused(call):
    assert any("hex" in p.lower() for p in code_guard.check(CLEAN + "\n" + call + "\n"))
