# Apo application icon — design brief

Canonical asset: [`apo-icon.png`](apo-icon.png) (512×512 transparent PNG).

## Symbol

Fountain pen nib — writing / notes — on a macOS-like rounded-square application tile.

## Visual constraints

- Shape: continuous-corner squircle (Mac app icon language only; **no** Apple logo or trademarked chrome)
- Palette: deep ink-blue enamel face; warm parchment / cream accents; restrained metal on the nib
- Motif: centered nib; subtle layered paper; optional faint search/focus ring
- Background: true alpha transparency outside the tile; never bake a checkerboard into the pixels
- No text, letters, watermarks, emoji, neon glow, or purple-on-white AI defaults
- Must remain legible when displayed at ~64–128 px in a GitHub README

## README usage

Centered, modest width (about 128 px). Decorates identity; does not replace the one-line product promise.

```html
<p align="center">
  <img src="docs/assets/apo-icon.png" alt="Apo" width="128" />
</p>
```

## Regeneration prompt

Use when regenerating from scratch:

> macOS-style application icon, 1024x1024, rounded square app tile with soft continuous-corner radius like a modern Mac app icon (shape language only — no Apple logo, no trademarked chrome). Deep ink-blue enamel front face with subtle specular highlight. Centered elegant fountain pen nib in warm gold and parchment cream, sharp and readable at small size. Behind the nib, a subtle motif of two slightly offset layered paper sheets and a faint circular search/focus ring suggesting knowledge retrieval. Soft drop shadow beneath the tile. True alpha-transparent canvas outside the tile — do not render or bake a checkerboard pattern. Dimensional but restrained — premium tool aesthetic, not cartoon, not neon, no text, no letters, no watermark.

## Selection note (2026-07-17)

Three candidates generated; **A** (ink-blue enamel + parchment paper + search ring) selected for closest match to this brief and strongest “application icon” read. Cream-tile and teal variants discarded from the tree to keep the share package lean.

Shipped file is **512×512** PNG (optimized for README payload; display width ~128 px). Regenerate at 1024 if you need App Icon / ICNS source masters.
