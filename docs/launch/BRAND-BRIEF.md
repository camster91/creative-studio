# Packstage brand brief

Draft for Bianca, 2026-10-14. Replaces the names "Photogen" and "Creative Studio".

## The one idea

Your real pack, on stage. Packstage puts a brand's actual packaging into new
scenes and gets it ready for every channel, without bending the label or
inventing claims.

## Who it's for

Small food, drink and wellness brands, roughly $1M–$20M in sales, with a 1–3
person marketing team and no in-house photographer. They launch flavours and
seasons often and need fresh images every month. They have been burned by AI
tools that warp logos or make up claims.

## Promise and proof

- **Promise:** studio-quality scenes where the label stays exactly right.
- **Proof:** we use the brand's real pack file, only approved claims, and
  export sized for Amazon, Shopify, Meta, Pinterest and email.
- **Done-for-you:** Ashbi can run the shoot for them.

## Voice

Plain, confident, a little dry. A good art director, not a tech startup.

| Say | Don't say |
| --- | --- |
| Your real label. Every time. | Revolutionary AI magic |
| Ready for Amazon, Shopify and Meta | Unlimited images |
| New flavour Monday, new photos Tuesday | Replace your photographer |
| We only use claims you've approved | Stunning, game-changing, effortless |

Write in sentence case. Buttons say exactly what happens: "Book a pilot",
"Make photos", "Download for Amazon".

## Tagline options

1. Your real pack, in every scene. (recommended)
2. Studio photos without the studio. Your label stays exact.
3. New flavour Monday. New photos Tuesday.

## Look and feel

The world we borrow from is the product photo studio: seamless paper
backdrops, gaffer tape, the 18% grey card photographers use to check exposure,
a cobalt backdrop, and one spotlight on the product.

| Token | Hex | Use |
| --- | --- | --- |
| Paper | #FBFBFA | Page background |
| Gaffer | #22252B | Text and wordmark |
| Quiet | #6B6F78 | Secondary text |
| Grey card | #8E9196 | Borders and rules only |
| Backdrop cobalt | #2B46D6 | The one accent: buttons, links, the carton |
| Spotlight | #FFD447 | Rare highlight under a product or headline |

All text colours pass WCAG AA on Paper. Tokens live in
`static/brand/tokens.css`.

**Type:** Archivo set wide (width 125, bold) for the wordmark and headlines;
Instrument Sans for everything else. Both are free Google fonts.

**Images do the talking.** Real packs on clean, light backgrounds. Lead with
before-and-after: the flat pack file beside the finished scene. No stock
photos, no AI people, no gradients.

**Layout:** generous white space, left-aligned text, one strong product image
per section. Cobalt shows up once or twice per screen, not everywhere.

## Wordmark concepts (pick one)

See `docs/launch/wordmark-concepts.png` and the SVGs in `static/brand/`.

| Concept | Idea | Strength | Watch out |
| --- | --- | --- | --- |
| A. Spotlight | The word stands on a yellow stage-light pool under "pack" | Warm, memorable, says "on stage" | Yellow needs care on white; the "p" crosses the light |
| B. Sweep | A cobalt seamless-paper curve rises behind the word | Most "photo studio", works as a frame for images | Wider shape; the curve alone could be the app icon |
| C. Carton | "pack" sits inside a cobalt box like a product; "stage" beside it | Clearest at small sizes, strong app icon (the cobalt box) | Splits the name into two words visually |

Recommendation: **C. Carton** for the app icon and favicon, because the cobalt
box reads at 16px. B is the strongest for the website hero if Bianca wants a
second lockup.

These are rough concepts built from Archivo outlines
(`scripts/brand/make_marks.py`). Bianca should refine spacing and the final
shapes before anything ships.

## Open items

- Bianca picks a concept and refines it.
- Domain: packstage.com, .ai and .co all answer but block automated checks,
  so they are probably registered. Check at a registrar; fallbacks are
  getpackstage.com, packstage.studio, packstage.app.
- Trademark search (Canada CIPO and US USPTO, classes 9, 35, 42) has not been
  run. The only use found so far is an old German packaging trade-fair name.
