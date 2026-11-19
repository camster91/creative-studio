# PhotoGen marketing site

A standalone static site for PhotoGen: plain HTML, one CSS file and a few lines of JS. No framework, no build step, no npm dependencies. It's separate from the Flask app so it can be deployed and changed on its own.

Brief and Brand Lock: [`docs/brand/PHOTOGEN-MARKETING-BRIEF.md`](../docs/brand/PHOTOGEN-MARKETING-BRIEF.md).

```
marketing-site/
  index.html                  Home
  pricing/index.html          Plans, credits per image, FAQ
  compare/photoroom/index.html
  404.html
  robots.txt  sitemap.xml  site.webmanifest
  assets/site.css             All styles
  assets/site.js              App URL (the one place to change it)
  assets/img/ assets/icons/   Higgsfield-generated images and icons
```

## Preview

Pages use root paths (`/assets/...`), so serve the folder as the web root. Either:

```sh
npx serve marketing-site
# or
python3 -m http.server 8080 --directory marketing-site
```

Then open the printed URL. Opening the HTML files straight from disk won't load styles.

## Deploy

Any static host works. Publish the `marketing-site/` folder as the site root and serve `404.html` for missing pages.

- **Cloudflare Pages:** build command none, output directory `marketing-site`. `404.html` is picked up automatically.
- **Coolify:** create a new application from this repo using the Static build pack, base directory `/marketing-site`, no build command. If auto-deploy is on, every push to `main` redeploys it.
- **Plain Nginx/Caddy on the VPS:** copy the folder and set `try_files $uri $uri/ =404; error_page 404 /404.html;`.

It's excluded from the app Docker image via `.dockerignore`.

## Change the app URL

All sign-up, sign-in and app links point at the PhotoGen app, `https://photogen.ashbi.ca` by default.

- **App URL:** edit `APP_URL` at the top of `assets/site.js`. Every link marked `data-app="/path"` is rewritten to `APP_URL + path` on load. The `href`s in the HTML hold the default so links still work without JS; if the app moves permanently, also run the find-and-replace below so crawlers see the new URL.
- **Canonical domain** (canonical tags, Open Graph URLs, JSON-LD, `robots.txt`, `sitemap.xml`): these must be in the HTML, so change them with one find-and-replace:

  ```sh
  grep -rl 'https://photogen.ashbi.ca' marketing-site | xargs sed -i 's#https://photogen.ashbi.ca#https://NEW-DOMAIN#g'
  ```

  If the marketing site takes the root domain and the app moves to `app.<domain>`, run that for the canonical domain, then set `APP_URL` and replace the `data-app` link `href`s with the app subdomain.

## Keep pricing in sync

- Plans and monthly credits must match `_BILLING_PLANS` in `scripts/creative-studio-web.py`.
- The credits-per-image table on `pricing/index.html` (and the "≈ images" figures and FAQ answers on the home and pricing pages) must match `CREDIT_WEIGHT_BY_TIER` in `billing.py` once PR #152 merges. Today that's Fast 1, Balanced 1, Quality 2, Ultra 5.
- Prices in the JSON-LD assume USD. Change `priceCurrency` if Stripe bills in another currency.

## Checks

No test suite. Before publishing, serve the folder and click through each page on mobile and desktop, and make sure every local `src`/`href` resolves.
