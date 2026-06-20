# Self-hosted fonts — RentBelgrad public catalog

The public site self-hosts three families (anti-slop law: no Inter/Roboto, real Cyrillic).
Files in this directory are **woff2 subsets pulled from Google Fonts** and served locally
via `@font-face` in `static/css/site.css` (referencing `/static/fonts/*.woff2`).
There is **no external `<link>` to fonts.googleapis** in `base.html`; the `@font-face`
fallback stack in `tokens.css` (`system-ui, sans-serif` / `ui-monospace`) covers any gap.

## Families / weights / subsets

| Family | Role (token) | Weights used | Subsets self-hosted |
|---|---|---|---|
| **Unbounded** | `--display` (headings, prices, logo) | 500, 600, 700, 800 | latin, latin-ext, cyrillic, cyrillic-ext |
| **Golos Text** | `--text` (body) | 400, 500, 600, 700 | latin, latin-ext, cyrillic, cyrillic-ext |
| **JetBrains Mono** | `--mono` (price/area/rooms, kickers) | 400, 500 | latin, latin-ext, cyrillic, cyrillic-ext |

All three are shipped by Google Fonts as **variable** woff2 files: a single woff2 per
family+subset carries the whole weight axis, so each `@font-face` block points at the same
file regardless of declared `font-weight`. That is why there are 4 files per family (one
per subset), not one per weight.

## Files

```
unbounded-{latin,latin-ext,cyrillic,cyrillic-ext}.woff2
golos-text-{latin,latin-ext,cyrillic,cyrillic-ext}.woff2
jetbrains-mono-{latin,latin-ext,cyrillic,cyrillic-ext}.woff2
```

## Source / how to refresh

Fetched from the Google Fonts CSS2 API with a modern browser User-Agent (so the API returns
woff2 + per-subset `unicode-range`), then each subset's woff2 downloaded directly:

```
https://fonts.googleapis.com/css2?family=Unbounded:wght@500;600;700;800&family=Golos+Text:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap
```

Licenses: Unbounded & Golos Text — OFL; JetBrains Mono — OFL. Redistribution of the woff2
subsets is permitted under the SIL Open Font License.

The `unicode-range` declarations in `site.css` mirror Google's subset ranges so the browser
only fetches the subset it needs (Cyrillic pages skip the larger Latin-ext file, etc.).
