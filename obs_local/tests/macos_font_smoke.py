"""Exercise macOS LibreOffice rendering with a temporary, unique font."""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from pptx import Presentation
from pptx.util import Inches, Pt

from pptx2png_core import converter_engine


def _create_font(path: Path, family: str, text: str) -> None:
    character_map = {
        ord(character): f"g{ord(character):04X}" for character in sorted(set(text))
    }
    glyph_order = [".notdef", *character_map.values()]
    glyphs = {}
    for glyph_name in glyph_order:
        pen = TTGlyphPen(None)
        if glyph_name != character_map.get(ord(" ")):
            pen.moveTo((80, 0))
            pen.lineTo((520, 0))
            pen.lineTo((520, 700))
            pen.lineTo((80, 700))
            pen.closePath()
        glyphs[glyph_name] = pen.glyph()

    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(character_map)
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({glyph: (600, 0) for glyph in glyph_order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({
        "familyName": family,
        "styleName": "Regular",
        "uniqueFontIdentifier": family,
        "fullName": family,
        "psName": family,
        "version": "Version 1.0",
    })
    builder.setupOS2(
        sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200
    )
    builder.setupPost()
    builder.setupMaxp()
    builder.save(path)


def main() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("The LibreOffice font profile smoke test requires macOS")
    with tempfile.TemporaryDirectory(prefix="pptx2png-macos-") as directory:
        root = Path(directory)
        fonts = root / "fonts"
        fonts.mkdir()
        family = f"Pptx2pngCi{uuid.uuid4().hex[:12]}"
        text = "FONT PROFILE TEST 123"
        _create_font(fonts / "ci-font.ttf", family, text)

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2))
        run = box.text_frame.paragraphs[0].add_run()
        run.text = text
        run.font.name = family
        run.font.size = Pt(24)
        source = root / "smoke-test.pptx"
        presentation.save(source)
        output = root / "png"
        output.mkdir()

        with (
            patch.dict(os.environ, {"PPTX2PNG_FONT_DIRS": str(fonts)}),
            patch.object(
                converter_engine,
                "_soffice_fallback_args",
                side_effect=AssertionError("Unexpected invisible fallback"),
            ),
        ):
            pdf = converter_engine.pptx_to_pdf_crossplatform(source, root / "pdf")
            with pymupdf.open(pdf) as document:
                font_names = {
                    font[3].split("+")[-1] for font in document[0].get_fonts()
                }
                if not any(family in name for name in font_names):
                    raise AssertionError(
                        f"Expected {family} in PDF, found: {font_names}"
                    )
                if text not in document[0].get_text():
                    raise AssertionError("Expected text was not rendered in PDF")

            png_paths, _ = asyncio.run(
                converter_engine.convert_all_pngs(source, output, "standard")
            )
        if len(png_paths) != 1 or not png_paths[0].is_file():
            raise AssertionError("Expected one rendered PNG slide")
        print("Unique profile font found in PDF; PNG rendering succeeded.")


if __name__ == "__main__":
    main()
