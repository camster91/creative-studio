# PhotoGen: design and marketing brief

Version 1, 2026-10-04. Repo: camster91/creative-studio (public, Flask). App: photogen.ashbi.ca. Scope: **the marketing site and assets only.** Another job owns credit metering and pricing logic in PR #152 (`fix/photogen-credit-metering`, which also edits `templates/landing.html`, `templates/terms.html` and the billing code). Don't touch those files. Build the marketing site as a separate static site in `marketing-site/` so the two PRs can't conflict.

## 1. Positioning

- **One-liner:** Studio-quality product photos for every channel, built from your real product and brand rules. Press Go.
- **Category shelf:** AI product photography for CPG/DTC brands (Pebblely, Photoroom, Claid, Shopify Magic).
- **Why PhotoGen wins:**
  - It starts from your brand and product truth, not a blank prompt. A **Brand Passport** holds colours, fonts, tone and must-never-show rules. **Product Truth** holds the exact SKU, verified facts and approved claims.
  - **One button.** Fill in a short work order (goal, audience, channels, style, number of variations). A readiness check shows anything missing, then you press **Go**.
  - **Every channel in one run:** square marketplace shots, 4:5 social, 2:3 Pinterest, 16:9 email and web banners.
  - **You review before you publish.** No automatic posting.
  - **Prompt Studio** gives direct control to anyone who wants to write prompts.
  - Your own Gemini key works free. Without one, you use credits.
- **Audience:**
  - Founders and e-commerce or marketing leads at lean CPG/DTC brands, typically 1–20 people, selling on Shopify, Amazon and Instagram. They need fresh listing, ad and social images every week and can't book a studio each time.
  - Secondary: small agencies and freelancers producing content for several brands (Ashbi's own clients).
- **Anti-persona:** brands that need regulated pack-compliance sign-off without human review; fashion on-model catalog work at scale.
- **Objections to answer:**
  - Will it change my packaging or label? It's built from your product photo. Always check the label before you publish; the app says this too.
  - Is this just a background remover? No. It's campaign sets with brand rules.
  - What does it cost per image? See the credits table.
  - Who owns the images? You do, subject to the provider terms. Check `templates/privacy.html` and the terms; don't over-claim.
  - Where does my data go? The owner-scoped database plus the image provider, per the Privacy page.

## 2. Voice

Confident, practical, a little playful, like a good studio producer. Short sentences, concrete outputs ("12 images, four sizes, one click"). No hype words ("revolutionary", "stunning", "AI-powered magic").

## 3. Visual direction (Brand Lock)

The concept is "the studio in a button". Colour-blocked seamless paper backdrops, the way a photo studio rolls out paper, and one big round cobalt **Go** button as the signature element. Everything else stays white and quiet so the product photos carry the page.

| Token | Hex | Role |
|---|---|---|
| Graphite | `#16181D` | Text, wordmark |
| White | `#FFFFFF` | Page background |
| Cobalt | `#2340FF` | The Go button, primary CTAs, links. White on Cobalt is 6.5:1 |
| Butter | `#FFE07A` | Backdrop panel (hero, OG) |
| Coral | `#FF8A6B` | Backdrop panel; never for text on white |
| Mint | `#8EE0C1` | Backdrop panel; never for text on white |
| Mist | `#F3F4F6` | Quiet section background, borders `#E4E6EA` |

- **Type:** **Archivo** (Google Fonts, variable, with a width axis). Headlines use Archivo at width 112–125, weight 800, tracking -0.02em. Body and UI use Archivo at width 100, weight 400/500. One family with a deliberate width contrast.
- **Shape:**
  - Photos sit in rounded 20px frames on colour panels.
  - The Go button is a true circle, 120px or more on desktop. Pressing it plays a subtle "shutter" scale effect (0.96, then 1).
  - Buttons are pill-shaped, with a 48px minimum height.
- **Motion:** one moment. In the hero, the Go button gives one gentle pulse after load. Clicking it scrolls to the "How it works" demo, or goes to signup. Respect `prefers-reduced-motion`.
- **Don'ts:**
  - Dark mode with neon accents.
  - Gradient blobs.
  - All-caps eyebrows on every section.
  - Fake logos or testimonials.
  - Numbered "01/02/03" markers, except for the real three-step sequence.

## 4. Site architecture (`marketing-site/`, static HTML/CSS, no build step)

```
/                    index.html: home (single long page with anchors)
/pricing/            pricing/index.html: plans, credits per image, FAQ
/compare/photoroom/  compare/photoroom/index.html (adapted from content/blog/photoroom-vs-photogen.md; keep claims factual)
/404.html
robots.txt, sitemap.xml, site.webmanifest, favicon set
```

- **Header:** logo, then How it works, Examples, Pricing, then Sign in (`https://photogen.ashbi.ca/login`). The primary CTA "Try it free" goes to `https://photogen.ashbi.ca/signup`.
- **Put the app base URL in one place:** a `data-app-url` attribute on `<body>`, with a tiny JS rewrite, or just a clear constant at the top of each page plus a README note. That way Cameron can change the domain once.
- **Footer:** Product (How it works, Pricing, Prompt Studio → `/app`), Compare, Company (Ashbi Design, Contact), Legal (Privacy → app `/privacy`, Terms → app `/terms`).

## 5. Home page copy

**Hero** (butter backdrop image right, text left; on mobile, text then image)
- H1: **Your product, shot for every channel. Press Go.**
- Sub: Upload one product photo, set your brand rules once, and PhotoGen produces studio, lifestyle and ad-ready images in every size you sell in. You review before anything goes live.
- Primary: the big round **Go** button, labelled "Try it free" for screen readers and visually "Go". Next to it, the text CTA "Try it free: 5 images on us". Both go to `/signup`.
- Secondary link: "See an example run", which goes to `#example`.
- Image: `hero-studio-set.webp`.

**Alternative headlines:**
- "A product photoshoot in one click."
- "Studio product photos without the studio."

**Before and after** (`#example`)
- H2: "One phone photo in. A whole campaign out."
- Left: `before-phone-snapshot.webp`, captioned "What you upload".
- Right: a 2×2 grid of `channel-marketplace-1x1`, `channel-instagram-4x5`, `channel-pinterest-2x3` and `channel-email-16x9`, each tagged with its size and use: "Marketplace 1:1", "Instagram 4:5", "Pinterest 2:3", "Email banner 16:9".
- Small print: "Example images created with PhotoGen's workflow for a sample product. Not a customer."

**How it works.** A real sequence, so numbers are allowed. Icons: brand-passport, product-truth, go.
1. **Set your brand once.** Colours, fonts, tone, and anything that must never appear.
2. **Add the product and what's true about it.** Exact SKU, verified facts and approved claims, so images never invent a benefit.
3. **Pick channels and press Go.** PhotoGen checks that nothing is missing, then makes the full set.

**Benefits.** Three, with icons channels, review and work-order:
- **Every size, one run.** Square, portrait, Pinterest and banner formats from a single brief.
- **On-brand by default.** Your rules ride along with every generation.
- **You stay in charge.** Review and refine before anything ships. Prefer prompts? Prompt Studio gives direct control.

**Range strip.** Three category images on their colour panels: `yuzu-can-studio` (drinks), `serum-mint` (beauty), `coffee-bag` (pantry). Caption: "Drinks, beauty, pantry, supplements: if it has a pack, PhotoGen can shoot it."

**Pricing teaser.**
- Starter $19/mo (100 credits).
- **Pro $49/mo (500 credits), recommended.**
- Studio $99/mo (1,500 credits).
- "5 free credits when you sign up. Bring your own Gemini key and generate without credits."
- Link to `/pricing/`.

**FAQ** (with FAQPage JSON-LD):
- Will PhotoGen change my label or packaging? It works from your product photo and your product facts, but AI can still get small details wrong. Check every image against the real pack before you publish.
- How is this different from a background remover? It makes complete campaign sets (studio, lifestyle and ad formats) from your brand rules and approved claims, not a single cut-out.
- What does an image cost? Simple images cost 1 credit, high-quality images 2, and Ultra images 5. A Pro plan covers about 500 everyday images a month.
- Can I use my own API key? Yes. Add your Gemini key in the app and generation doesn't use credits.
- Where does my data go? Your brand and product records stay in your PhotoGen account. Images and prompts are sent to the image provider to generate results. See the Privacy page.

**Final CTA.** Coral backdrop.
- H2: "Your next product shoot is one click away."
- The Go button.
- "5 free images. No card required." (Verify against the signup flow: a magic link plus 5 trial credits. Don't promise "no card" if signup asks for one.)

## 6. Pricing page

- Plans: Starter $19 (100 credits), Pro $49 (500, recommended), Studio $99 (1,500). Monthly, cancel anytime through the Stripe customer portal. Mention the portal only because it exists in code.
- Credits-per-image table, taken from PR #152's `CREDIT_WEIGHT_BY_TIER`:

  | Tier | Credits per image |
  |---|---|
  | Fast | 1 |
  | Balanced | 1 |
  | Quality | 2 |
  | Ultra | 5 |

- Add an HTML comment noting that it must match `billing.py` once #152 merges.
- Show "≈ images per month" for each plan at Balanced and at Ultra.
- Include the BYO-key note, the FAQ, and `Product` + `Offer` JSON-LD.
- No annual toggle (none exists in code).

## 7. SEO

- **Home title:** "PhotoGen: AI Product Photography for DTC & CPG Brands" (≤60 characters).
- **Meta description:** "Turn one product photo into studio, lifestyle and ad-ready images for Amazon, Shopify, Instagram and email. Set your brand rules once and press Go."
- **Keywords:** AI product photography, product photos for Shopify, Amazon product images, AI product photoshoot, CPG product photography.
- **Canonical:** use a placeholder origin from one config (`https://photogen.ashbi.ca` for now), noted in the README.
- **OG:** `og.jpg`. Also set `twitter:card` to `summary_large_image`.
- **JSON-LD:** `SoftwareApplication` with offers, plus `FAQPage`.
- **Images:** WebP with width/height set, lazy-loaded below the fold. Preload the hero.
- **Performance:** keep the whole page under ~1.5 MB on first load. No JS framework.

## 8. Asset list (`/workspace/brand-assets/photogen/`)

All made with Higgsfield. The sample product "Yuzu Tonic" is fictional.

| File (web/) | Use |
|---|---|
| `logo-a-aperture-go.svg` | **Recommended mark:** aperture iris with a Go triangle. Pair it with the wordmark "PhotoGen" set in Archivo Expanded 800 (needs Cameron's approval) |
| `logo-b-sweep-tile.svg`, `logo-c-shutter-p.svg` | Alternatives |
| `icon-512.png`, `icon-192.png`, `apple-touch-icon.png`, `favicon-32.png` | Favicons from logo A |
| `hero-studio-set.webp` | Hero |
| `before-phone-snapshot.webp` | "What you upload" |
| `channel-marketplace-1x1.webp`, `channel-instagram-4x5.webp`, `channel-pinterest-2x3.webp`, `channel-email-16x9.webp` | Channel set |
| `yuzu-can-studio.webp`, `serum-mint.webp`, `coffee-bag.webp` | Range strip |
| `brand-passport.svg`, `product-truth.svg`, `work-order.svg`, `go.svg`, `channels.svg`, `review.svg` | Icons (Graphite + Cobalt) |
| `og.jpg` | 1200×630 share card |
