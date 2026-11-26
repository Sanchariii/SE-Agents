# Rockwell icon library

Pre-built icons the slide designer places with `add_icon(name, ...)` instead of drawing
its own (`app/genai/ppt/icons.py`). Empty until the approved icon set is added.

Each icon is one **single-colour PNG with a transparent background** (any colour; it is
recoloured to a brand token when placed), listed in `index.json`:

```json
[{"name": "padlock", "file": "padlock.png",
  "keywords": ["lock", "lockout", "loto", "security"], "category": "safety"}]
```

- `name`: what the designer types; lower-case, hyphens for spaces.
- `keywords`: words a slide might use for it; the designer is offered the 5 icons whose
  name or keywords best match the slide's text.
- Keep icons square-ish and at least 128 px.
