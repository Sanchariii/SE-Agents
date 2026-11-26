"""Pre-built Rockwell icons: the designer places them, it never draws its own.

The library is a folder of single-colour PNG icons (transparent background) with an
``index.json`` beside them::

    [{"name": "factory", "file": "factory.png",
      "keywords": ["plant", "manufacturing", "site"], "category": "places"}]

``IconLibrary.search`` matches a slide's words against names and keywords and offers
the designer only the few best icons, so the prompt stays small. ``stage`` copies
those into the slide's sandbox, where ``DeckBuilder.add_icon(name, ...)`` places one
and recolours it to a brand token. With an empty library nothing is offered and every
prompt is exactly what it was before icons existed.
"""

import json
import re
import shutil
from pathlib import Path

from app.config.constants import PptConstants
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

ICON_DIRNAME = "icons"                     # under the template root and in a sandbox
_STOP_WORDS = {"the", "and", "for", "with", "your", "you", "are", "this", "that",
               "from", "into", "each", "every", "when", "what", "how", "why", "slide"}


class Icon:
    """One library icon."""

    def __init__(self, name, path, keywords=(), category=""):
        self.name, self.path = name, Path(path)
        self.keywords = [k.lower() for k in keywords]
        self.category = category

    def words(self):
        return {self.name.lower(), *re.split(r"[\s_-]+", self.name.lower()),
                *self.keywords}


class IconLibrary:
    """The icon index of one folder, searchable by words."""

    _default = None

    def __init__(self, folder):
        self.folder = Path(folder)
        self.icons = self._load()

    @classmethod
    def default(cls):
        """The library under the brand asset root (``company-ppt-templet/icons``)."""
        if cls._default is None:
            from app.genai.ppt.template import registry

            cls._default = cls(registry.template_root() / ICON_DIRNAME)
        return cls._default

    def _load(self):
        index = self.folder / "index.json"
        if not index.is_file():
            log.info("icon library %s has no index.json; no icons are offered",
                     self.folder)
            return []
        icons = []
        for entry in json.loads(index.read_text(encoding="utf-8")):
            path = self.folder / str(entry.get("file") or "")
            if not entry.get("name") or not path.is_file():
                log.warning("icon %r skipped: its file %s is missing", entry.get("name"),
                            path.name)
                continue
            icons.append(Icon(entry["name"], path, entry.get("keywords") or (),
                              entry.get("category") or ""))
        log.info("icon library loaded: %d icon(s) from %s", len(icons), self.folder)
        return icons

    def search(self, text, limit=PptConstants.ICONS_PER_SLIDE):
        """The icons whose name or keywords the text mentions, best first."""
        if not self.icons or not text:
            return []
        said = {w for w in re.findall(r"[a-z0-9]+", text.lower())
                if len(w) > 2 and w not in _STOP_WORDS}
        said |= {w[:-1] for w in said if w.endswith("s")}          # plurals
        scored = []
        for icon in self.icons:
            hits = said & icon.words()
            if hits:
                scored.append((len(hits) + (icon.name.lower() in said), icon.name, icon))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [icon for _, _, icon in scored[:limit]]

    @staticmethod
    def stage(icons, slide_dir):
        """Copy the offered icons into a slide's sandbox (``<slide_dir>/icons``)."""
        if not icons:
            return
        target = Path(slide_dir) / ICON_DIRNAME
        target.mkdir(parents=True, exist_ok=True)
        for icon in icons:
            shutil.copyfile(icon.path, target / f"{icon.name}.png")

    @staticmethod
    def prompt_block(icons):
        """The designer's icon menu, or "" when there is nothing to offer."""
        if not icons:
            return ""
        lines = [f"- {icon.name}" + (f" ({', '.join(icon.keywords[:4])})"
                                     if icon.keywords else "") for icon in icons]
        return ("\n\nROCKWELL ICONS you may place (pre-built - never draw an icon "
                "yourself). Use s.add_icon(name, left, top, size=0.6, color='red') with "
                "a name from this list, only where an icon helps the reader:\n"
                + "\n".join(lines))
