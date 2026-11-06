"""Throwaway check that DeckStitcher merges generated slides (images included)."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptx import Presentation
from pptx.util import Emu, Inches, Pt

from app.config.constants import PptCreatorConstants
from app.genai.ppt.stitcher import DeckStitcher


def make_slide(path: Path, text: str, image: Path | None = None) -> None:
    prs = Presentation()
    prs.slide_width = Emu(PptCreatorConstants.SLIDE_WIDTH_EMU)
    prs.slide_height = Emu(PptCreatorConstants.SLIDE_HEIGHT_EMU)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    run = box.text_frame.paragraphs[0].add_run()
    run.text = text
    run.font.size = Pt(24)
    if image:
        slide.shapes.add_picture(str(image), Inches(1), Inches(3), Inches(2), Inches(2))
    slide.notes_slide.notes_text_frame.text = f"notes for {text}"
    prs.save(str(path))


def main() -> None:
    tmp = Path(__file__).resolve().parent / "_stitch_tmp"
    tmp.mkdir(exist_ok=True)

    png = tmp / "dot.png"
    png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
        "00000049454e44ae426082"))

    files = []
    for index in (1, 2, 3):
        path = tmp / f"slide_{index}.pptx"
        make_slide(path, f"Slide {index}", png if index == 2 else None)
        files.append((index, path))

    out = tmp / "deck.pptx"
    stitcher = DeckStitcher()
    stitcher.stitch(list(reversed(files)), out)

    merged = Presentation(str(out))
    titles = [shape.text_frame.text for slide in merged.slides
              for shape in slide.shapes if shape.has_text_frame]
    pictures = [shape for slide in merged.slides for shape in slide.shapes
                if shape.shape_type == 13]
    notes = [slide.notes_slide.notes_text_frame.text for slide in merged.slides]
    print("slides:", len(merged.slides.__iter__.__self__._sldIdLst))
    print("titles:", titles)
    print("pictures:", len(pictures), [p.image.blob[:4] for p in pictures])
    print("notes:", notes)
    print("size:", merged.slide_width, merged.slide_height)
    print("stitch notes:", stitcher.notes)


if __name__ == "__main__":
    main()
