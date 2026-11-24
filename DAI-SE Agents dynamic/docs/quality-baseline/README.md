# Deck quality baseline

Run the same set before and after every feature change; a deck without a reference must be no worse than baseline.

**Set (on `main` first):** deck types training, quiz, FAQ, job aid, overview x creativity MEDIUM and HIGH (+ LOW once).
Same two source documents, same prompt and same slide-count answer every time.

**Prompts** (`prompt_id`):
- `P1` Create a {deck_type} on lockout/tagout safety for new maintenance technicians from the attached documents.
- Answer the size question with `10-15`.

**Record** one row per deck in `baseline-results.csv`:
- `gate_pass_pct`, `tier2_fallbacks`, `tier3_fallbacks`: from the run's progress events / logs
- `max_mode_avg_score`, `max_mode_redos`: HIGH only, from the slide reviewer log lines
- `tokens`, `cost_usd`: Analytics tab
- `score_*`: a person's 1-5 rating after opening the deck
- keep every `.pptx` and put its file name in `deck_file`

**Pass rule after a change (no reference):** gate pass rate equal or higher; tier-3 fallbacks at most +5 points; max-mode score not lower; tokens and time at most +15%; manual scores not lower.
