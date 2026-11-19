# Process-settings metadata and translations

The schema, UI tree and toggle rules are extracted from OrcaSlicer through
`frontend/scripts/generate-slicer-schema.mjs`. Keep their keys, enum values,
defaults, constraints and order in the original slicer form.

`process-uk.json` contains display text only, selected from the official
[OrcaSlicer Ukrainian gettext catalog](https://github.com/OrcaSlicer/OrcaSlicer/blob/e2c2641acf18a1e466a833dba3c890496e24afd2/localization/i18n/uk/OrcaSlicer_uk.po).
Source commit: `e2c2641acf18a1e466a833dba3c890496e24afd2`.
Catalog SHA-256: `b118af1b5d053d9deb4355bd5af7d65343785f0ff68ab0ab0ecc75a633b579a4`.
Translations are by the OrcaSlicer Ukrainian translation contributors, including
the catalog's credited translator Andrij Mizyk; reused under OrcaSlicer's
AGPL-3.0 license, compatible with BamDude. Existing English text remains the
fallback. No catalog fetch or gettext parser runs on the printer farm/browser.

Regenerate from the repository root (development tool only):

```sh
python -m pip install polib==1.2.0
python scripts/sync_slicer_translations.py
python scripts/sync_slicer_translations.py --catalog /path/to/OrcaSlicer_uk.po --check
```

The source pin and checksum must be reviewed together when updating. The generator
uses polib rather than a custom PO parser; it ignores fuzzy/obsolete, plural and
contextual entries. Identical words with extractor-only escaping or whitespace
differences can match; approximate text matching is never used.

`process-uk-overrides.json` holds reviewed supplements for descriptions that
changed between the vendored schema and the pinned catalog, plus an upstream
label typo. Each is keyed by the complete original text so a schema update
cannot silently reuse a translation describing different behavior. Regeneration
fails on missing coverage or unused supplements. Review numerical limits and
conditions especially carefully: newer Orca descriptions can differ from the
schema even when their first sentence is identical.

The panel loads this catalog alongside its metadata and translates display text,
tooltips and enum labels only. Search accepts translated text and English keys.
Language changes preserve edited values; the submitted process settings retain
their original keys and values. Printer and user preset names are not translated.
