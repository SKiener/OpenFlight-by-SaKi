---
icon: lucide/palette
---

# UI Colour Palette

The canonical OpenFlight interface palette. Use these tokens when adding or
changing UI, rather than introducing new values.

Referenced from
[`CONTRIBUTING.md`](https://github.com/jewbetcha/openflight/blob/main/CONTRIBUTING.md).


## Backgrounds

| | Name | Hex | CSS variable |
| --- | --- | --- | --- |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#0a0a0f;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Deep Background | `#0A0A0F` | `--color-bg-deep` |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#12121a;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Card Background | `#12121A` | `--color-bg-card` |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#1a1a24;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Elevated | `#1A1A24` | `--color-bg-elevated` |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#222230;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Hover State | `#222230` | `--color-bg-hover` |

## Gold (Primary)

| | Name | Hex | CSS variable |
| --- | --- | --- | --- |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#D4AF37;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Gold | `#D4AF37` | `--color-gold` |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#F4CF47;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Gold Bright | `#F4CF47` | `--color-gold-bright` |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#A68B2A;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Gold Dim | `#A68B2A` | `--color-gold-dim` |

## Cream (Text)

| | Name | Hex | CSS variable |
| --- | --- | --- | --- |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#F5F0E6;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Cream | `#F5F0E6` | `--color-cream` |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:rgba(245, 240, 230, 0.7);border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Cream Dim | `rgba(245, 240, 230, 0.7)` | `--color-cream-dim` |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:rgba(245, 240, 230, 0.5);border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Cream Muted | `rgba(245, 240, 230, 0.5)` | `--color-cream-muted` |

## Accents

| | Name | Hex | CSS variable |
| --- | --- | --- | --- |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#4ADE80;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Success | `#4ADE80` | `--color-success` |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#60A5FA;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Info | `#60A5FA` | `--color-info` |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#FBBF24;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Warning | `#FBBF24` | `--color-warning` |
| <span style="display:inline-block;width:1.15rem;height:1.15rem;border-radius:3px;background:#F87171;border:1px solid rgba(128,128,128,.45);vertical-align:-0.22rem"></span> | Danger | `#F87171` | `--color-danger` |

## Dispersion chart (club colours)

The companion's Streuung chart colours clubs by their position in the bag
(shortest first), cycling these eight slots. They are the categorical slots of
the data-viz reference palette, checked with its validator on the kiosk
surfaces (`#000000` dark, `#f4f2ec` light): neighbouring slots, including the
wrap from 8 to 1, clear the colour-blind separation target in both themes.
Four light slots sit below 3:1 contrast, so the chart always pairs colour with
labels, a legend and the per-club table. Clubs outside the bag use
`--color-text-faint`.

| Slot | Dark | Light | CSS variable |
| --- | --- | --- | --- |
| 1 | `#3987E5` | `#2A78D6` | `--dispersion-1` |
| 2 | `#D95926` | `#EB6834` | `--dispersion-2` |
| 3 | `#199E70` | `#1BAF7A` | `--dispersion-3` |
| 4 | `#C98500` | `#EDA100` | `--dispersion-4` |
| 5 | `#D55181` | `#E87BA4` | `--dispersion-5` |
| 6 | `#008300` | `#008300` | `--dispersion-6` |
| 7 | `#9085E9` | `#4A3AA7` | `--dispersion-7` |
| 8 | `#E66767` | `#E34948` | `--dispersion-8` |

## Using these

The same palette drives the docs site — see
`docs/stylesheets/extra.css`, where the gold, cream, and near-black values
are mapped onto the theme's tokens.

When adding UI, prefer an existing token over a new hex value. If a genuinely
new colour is needed, add it here in the same commit so this page stays the
single reference.
