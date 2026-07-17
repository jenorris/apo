# Apo application icon — design brief

Canonical asset: [`apo-mark.png`](apo-mark.png) (512×512 transparent PNG).

## Symbol

Fountain pen nib — writing / notes — on a macOS-like rounded-square application tile.

## Visual constraints

- Shape: continuous-corner squircle (Mac app icon language only; **no** Apple logo or trademarked chrome)
- Palette: deep ink-blue enamel face; warm parchment / cream accents; restrained metal on the nib
- Motif: centered nib; subtle layered paper; optional faint search/focus ring
- Background: true alpha transparency outside the tile; never bake a checkerboard or light-gray studio backdrop into the pixels
- Prefer a hard cutout (opaque tile + transparent canvas). Avoid soft drop shadows in the README asset — they pick up backdrop fringing and GitHub camo caches old URLs
- No text, letters, watermarks, emoji, neon glow, or purple-on-white AI defaults
- Must remain legible when displayed at ~64–128 px in a GitHub README

## README usage

Centered, modest width (about 128 px). Decorates identity; does not replace the one-line product promise.

```html
<p align="center">
  <img src="docs/assets/apo-mark.png" alt="Apo" width="128" />
</p>
```

Rename the file (e.g. `apo-mark.png` → `apo-mark-v2.png`) when replacing the asset so GitHub’s image CDN does not keep serving a stale blob.

## Regeneration prompt

Use when regenerating from scratch:

> macOS-style application icon, 1024x1024, rounded square app tile with soft continuous-corner radius like a modern Mac app icon (shape language only — no Apple logo, no trademarked chrome). Deep ink-blue enamel front face with subtle specular highlight. Centered elegant fountain pen nib in warm gold and parchment cream, sharp and readable at small size. Behind the nib, a subtle motif of two slightly offset layered paper sheets and a faint circular search/focus ring suggesting knowledge retrieval. Flat cutout on a TRUE transparent alpha canvas — no drop shadow, no checkerboard, no light-gray backdrop, no studio floor. Dimensional but restrained — premium tool aesthetic, not cartoon, not neon, no text, no letters, no watermark.

## Selection note (2026-07-17)

Three candidates generated; **A** (ink-blue enamel + parchment paper + search ring) selected. Shipped README mark is a hard-edged transparent cutout (`apo-mark.png`) derived from that art — gray studio backdrop and soft-shadow fringe removed; filename changed from `apo-icon.png` to bust GitHub camo cache.
