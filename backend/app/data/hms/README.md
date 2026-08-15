# HMS error descriptions

Generated from BambuStudio by `scripts/import_hms_catalogue.py`. **Do not edit by
hand** — re-run the script instead.

- **Source:** BambuStudio `resources/hms/hms_en_{prefix}.json` @ tag `v02.08.02.60`
- **One file per model prefix** — the first three characters of a serial number,
  the same key `hms_actions.json` uses.
- **36 522 entries across 7 models, ~4.4 MB.**

## Why per model, and not deduplicated

Only 5 289 of those codes are unique; the rest repeat across models. Merging them
would be smaller and **wrong**: **879 codes carry different text on different
machines**. `0C00020000010001` is "The horizontal laser is not lit" on one and
"The height measuring laser is not lit" on another — two mechanisms behind one
number. A shared entry would describe the wrong one with full confidence.

⚠️ Deduplication belongs to the translation pipeline, never to storage. The same
sentence appearing seven times costs disk; the same sentence *translated* seven
times costs five times the work for nothing. See `scripts/translate_hms_catalogue.py`.

## Two key shapes, both required

| BS section | key length | example |
|---|---|---|
| `device_hms` | **16** hex chars | `050002000002000B` |
| `device_error` | **8** hex chars | `0580409C` |

⚠️ This is the mistake this data replaced. BamDude's old hardcoded table came
from the short-code half only — it intersected `device_error` in 692 places and
`device_hms` in **zero**, while `device_hms` is the half printers report. A
printer refusing to record a timelapse because its card was full reported
`0500010000030004`, and BamDude had no text for it anywhere.

Both halves are imported into one flat map per model. Lookup tries the full code
first (lossless), then the short one — see `services/hms_catalogue.py`.

## Ukrainian

BambuStudio ships 16 languages and **`uk` is not among them**, so ours is
generated: `uk/{prefix}.json`, produced by `scripts/translate_hms_catalogue.py`.
A code with no translation falls back to English rather than to a blank.

## Re-syncing

1. Refresh the checkout: `git -C temp/references/BambuStudio fetch --tags`, then
   check out the newest tag.
2. `python scripts/import_hms_catalogue.py`
3. `python scripts/translate_hms_catalogue.py --export` / `--import` for strings
   the refresh added.
4. Update the tag above and record the sync in `temp/bs-reference-audits/`.
