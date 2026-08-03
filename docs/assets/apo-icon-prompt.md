# Apo application icon — design brief

Canonical asset: [`apo-icon.png`](apo-icon.png) (1024×1024 PNG). Same mark as macOS Notification Center toasts (`Apo Notify.app` / Meta `system/assets/apo-notify-icon.png`).

## Symbol

Diagonal fountain-pen quill + nib — writing / notes — on a macOS-like rounded-square application tile.

## Visual constraints

- Shape: continuous-corner squircle (Mac app icon language only; **no** Apple logo or trademarked chrome)
- Palette: near-black charcoal tile; solid amber / orange-gold glyph (`#F9B233`-class)
- Motif: flat quill feather (three right-edge barbs) joined to a classic nib with ferrule band; no gradients, no metallic skeuomorphism
- Background: opaque tile face (toast/app-bundle source is RGB). Prefer matching the notify icon byte-for-byte rather than regenerating a near-miss
- No text, letters, watermarks, emoji, neon glow, or purple-on-white AI defaults
- Must remain legible when displayed at ~64–128 px in a GitHub README and at notification badge size

## README usage

Centered, modest width (about 128 px). Decorates identity; does not replace the one-line product promise.

```html
<p align="center">
  <img src="docs/assets/apo-icon.png" alt="Apo" width="128" />
</p>
```

Rename the file (e.g. `apo-icon.png` → `apo-icon-v2.png`) when replacing the asset so GitHub’s image CDN does not keep serving a stale blob.

## Regeneration prompt

Use when regenerating from scratch (prefer re-copying `apo-notify-icon.png` when aligning with toasts):

> macOS-style application icon, 1024x1024, rounded square app tile with soft continuous-corner radius like a modern Mac app icon (shape language only — no Apple logo, no trademarked chrome). Flat matte near-black charcoal face. Centered diagonal amber-gold fountain-pen quill: feather with three curved barbs sweeping upper-right, thin ferrule band, classic nib with slit and breather hole pointing lower-left. Solid flat fills only — no gradient, no metallic highlight, no paper sheets, no search ring, no drop shadow, no checkerboard, no studio backdrop. Premium tool mark, not cartoon, not neon, no text, no letters, no watermark.

## Selection note (2026-08-03)

README previously shipped a skeuomorphic ink-blue + parchment nib cutout (`apo-mark.png`, ex-`apo-icon.png`) generated for the 2026-07-17 share package. Canonical identity is the flat amber quill used by **Apo Notify** toasts; README asset restored to `apo-icon.png` as a byte-identical copy of Meta `system/assets/apo-notify-icon.png`.
