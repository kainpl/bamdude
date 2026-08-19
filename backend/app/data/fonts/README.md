# Label fonts

Fonts used by `backend/app/services/label_renderer.py`. Nothing else reads them.

## Why they are here at all

reportlab's built-in Type-1 faces (`Helvetica`, `-Bold`, `-Oblique`) are
WinAnsi-encoded. Handed a character they cannot encode they do **not** raise —
they quietly switch to `ZapfDingbats`, whose `n` glyph is a filled black square.
So a spool named `Чорний матовий` in location `Полиця 3` printed as
`■■■■■■ ■■■■■■■` and `■■■■■■ 3`, on every template, with nothing in the log and
Latin text entirely unaffected. `uk` is one of BamDude's two first-class
locales.

## Why Arimo and not something else

The label layout truncates text on `stringWidth`, and every font size in it was
chosen against Helvetica's metrics. A wider face does not merely look different
— it starts clipping names that fit today, worst on the 40×30 mm template where
the text column is narrowest.

Arimo is metric-compatible with Arial, and therefore with Helvetica. Measured
before adopting it, across representative label strings:

| face | Arimo vs Helvetica | DejaVu Sans vs Helvetica |
|---|---|---|
| regular | ±0.1 % | +5 % … +25 % |
| bold | ±0.1 % | +13 % … +18 % |
| italic | ±0.1 % | — |

DejaVu Sans was the obvious candidate (it already ships inside matplotlib, which
is a dependency, so it would have cost nothing to reach). It was rejected on
those numbers: adopting it would have been a silent layout regression traded for
a font fix.

## Provenance

Upstream: `github.com/googlefonts/Arimo`, `fonts/ttf/`, unmodified.
Licence: SIL Open Font License 1.1 — see `OFL.txt`, which must stay beside the
font files. The copyright line carries no Reserved Font Name, so the faces are
registered under their real names and the PDF names them honestly.

`Arimo-BoldItalic.ttf` is deliberately **not** vendored — the layout has no
bold-italic run, and an unused megabyte is still a megabyte in every image.

## Re-syncing

Re-download the three `.ttf` files and `OFL.txt` from the same paths upstream.
If a future version drifts from Arial metrics, `test_unicode_fonts_are_shipped_and_registered`
will not catch it — the layout tests
(`test_box_template_does_not_truncate_normal_brand_or_name`) are what notice.
