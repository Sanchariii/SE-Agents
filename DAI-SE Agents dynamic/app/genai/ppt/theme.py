"""Deck themes: palette + typography applied consistently by the renderer."""

from dataclasses import dataclass
from pptx.dml.color import RGBColor


@dataclass(frozen=True)
class Theme:
    name: str
    primary: str
    secondary: str
    accent: str
    tint: str
    dark_text: str = "212121"
    light_text: str = "FFFFFF"
    muted_text: str = "5A5A5A"
    background: str = "FFFFFF"

    def rgb(self, hex_color: str) -> RGBColor:
        return RGBColor.from_string(hex_color)


THEMES: dict[str, Theme] = {
    "midnight":   Theme("midnight",   "1E2761", "CADCFC", "8FA9F0", "EEF2FC"),
    "teal":       Theme("teal",       "026873", "7FD4C8", "4FC3AF", "E7F5F3"),
    "forest":     Theme("forest",     "2C5F2D", "C6DDB4", "97BC62", "EFF5E9"),
    "ocean":      Theme("ocean",      "065A82", "A9CBDD", "6FAECB", "EAF2F7"),
    "terracotta": Theme("terracotta", "9E4034", "E7C9BF", "D08C7A", "FAEFEB"),
    "charcoal":   Theme("charcoal",   "36454F", "C3D0D8", "F2A65A", "EFF2F4"),
    "berry":      Theme("berry",      "6D2E46", "DCC0C8", "C08A98", "F8EFF2"),
}

DEFAULT_THEME = "midnight"
TITLE_FONT, BODY_FONT = "Cambria", "Calibri"
SLIDE_W, SLIDE_H, MARGIN = 13.333, 7.5, 0.6


def get_theme(name: str | None) -> Theme:
    return THEMES.get((name or "").lower(), THEMES[DEFAULT_THEME])