"""Remove listing-platform watermarks (Halo Oglasi, 4zida, CityExpert) from apartment photos.

Public API lives in `src.watermark.pipeline`: `clean_array`, `clean_bytes`, `clean_file`.
Watermark templates are in `src/watermark/assets/` (coverage PNG + geometry JSON); how they
were measured is described in docs/superpowers/plans/2026-10-14-watermark-remover.md.
"""
