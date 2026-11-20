# Deck polish agent

Reviews the FINISHED deck - from either engine - and makes it look better, as far as
the creativity level the user picked allows. Switch it on in `app/config/dev.env`:

    DECK_POLISH=on        follow the user's Low / Medium / High choice
    DECK_POLISH=medium    always one level (low | medium | high) - handy for testing
    DECK_POLISH=off       the default

| Level | What it does | Model call |
|---|---|---|
| Low - clean-up | removes a stamped label or slide number the template already prints, pulls shapes back onto the slide, lifts text to the 10 pt floor, swaps off-brand colours for the nearest brand colour, US spelling | no |
| Medium - polish | Low, plus lifts column headings that sit under their columns, raises small component text to 12 pt where it fits, then one model round: fix overlaps and walls of text, turn plain lists into cards or numbered steps, add a stat tile for a key figure | yes |
| High - showcase | Medium, plus timelines and up to two section dividers, one visual language across the deck | yes |

How it stays safe:

* The model never writes code or colours. It answers with operations from a fixed menu
  (`ops.py`); code checks each one and draws the result in the brand palette.
* It may not invent a figure, drop a fact (removed text goes into the notes), or touch
  the frame or anything the template placed on its own graphics.
* The deck is scored before and after; a round that scores worse is thrown away. A
  failure publishes the deck as built.

## Files

| File | What it does |
|---|---|
| `engine.py` | the setting, the level, `polish_deck` |
| `checks.py` | what is wrong, and who can fix it (rule / model / a person) |
| `rules.py` | the fixes code makes on its own |
| `ops.py` | the model's operations, validated and applied |
| `prompts.py`, `skills/polish-*/SKILL.md` | one skill per level, plus the ppt skill's brand sections |
| `graph.py` | inspect -> rules -> (model -> apply -> judge) -> finish |
| `reader.py`, `spelling.py` | reading shapes; US spelling |

## Try it on any deck

    python -m scripts.developer_tools.deck_polish "classic.pptx" --level low
    python -m scripts.developer_tools.deck_polish "classic.pptx" --level medium --save-ops ops.json
