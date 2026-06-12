"""Comparison data: Photogen vs each major competitor. Honest, no vapor.

Each entry: competitor slug, display name, tagline, the rows to render
in the side-by-side table, the verdict paragraph, and 4-6 FAQs that
real buyers ask when comparing the two products.
"""

COMPETITORS = {
    "flair": {
        "name": "Flair.ai",
        "url": "https://flair.ai",
        "tagline": "Drag-and-drop AI editor, on-model photography, subscription",
        "rows": [
            ("Pricing model", "Pay per image ($0.02–$0.24), BYOK free", "Subscription $24–$288/mo + per-image credits"),
            ("Bring your own API key", "✅ Yes — first-class", "❌ No — they call Google on your behalf"),
            ("Self-hostable", "✅ Single Docker container, MIT-friendly code", "❌ Closed-source SaaS only"),
            ("Pricing floor (cheapest tier)", "$0.02/image = $0.50 for a 25-image set", "$24/mo minimum before any generation"),
            ("Batch 4-up variations", "✅ One click, ~2 min", "✅ Yes, on Pro+ plans"),
            ("Aspect ratios", "6 (1:1, 4:5, 9:16, 16:9, 2:3, 4:3)", "5 (no 4:3)"),
            ("On-model photography", "❌ Not yet — roadmap Q3 2026", "✅ Yes, on Professional+ plans"),
            ("Product compositing (no hallucinated labels)", "✅ Composite pipeline — your PNG is composited into the scene", "❌ Generates from scratch, can hallucinate labels"),
            ("REST API", "✅ Yes, public docs at /docs", "❌ No public API"),
            ("Per-user image library", "✅ Yes, with filter & search", "✅ Yes, on Pro+ plans"),
            ("Cost cap / day limit", "Configurable per-user, default $5", "Hard plan limits, no daily cap"),
        ],
        "verdict": """<p>Pick <strong>Flair</strong> if you need on-model photography now, want a polished
drag-and-drop editor, and don't mind paying $24–$288/mo for the privilege. They have
real product-market fit and a real design team.</p>
<p>Pick <strong>Photogen</strong> if you (a) want to use your own Gemini key and not pay
us anything, (b) want to self-host the whole app on your own infrastructure, (c)
need a public REST API, or (d) want to pay per image instead of being on a plan.</p>
<p>Both produce comparable image quality. The difference is in the business model
and the deployment story.</p>""",
        "faqs": [
            ("Is Flair.ai better quality than Photogen?", "Roughly the same. Both use Google's image generation models under the hood. Flair has a slight edge on on-model photography because they've been tuning that path longer. For product-only shots (no human model), they're a wash."),
            ("Can I switch from Flair to Photogen?", "Yes. The output is a PNG either way. Drop your existing product photos into Photogen, paste a Gemini key, generate the same scenes. Most teams run both in parallel for a few weeks before cutting over."),
            ("Does Photogen offer on-model photography?", "Not yet. It's on the Q3 2026 roadmap. In the meantime, use Photogen for product-only and another tool for on-model."),
            ("Is Photogen's API really public?", "Yes. See <a href='/docs'>/docs</a>. You can generate from curl, Zapier, Make, or any custom app. Flair has no public API."),
        ],
    },
    "pebblely": {
        "name": "Pebblely",
        "url": "https://pebblely.com",
        "tagline": "AI product photo generator, batch background generation",
        "rows": [
            ("Pricing model", "Pay per image ($0.02–$0.24), BYOK free", "Subscription $13–$39/mo + image credits"),
            ("Bring your own API key", "✅ Yes", "❌ No"),
            ("Self-hostable", "✅ Yes", "❌ No"),
            ("Pricing floor", "$0.02/image", "$13/mo minimum"),
            ("Batch background generation", "✅ One click, 4-up", "✅ Yes"),
            ("Aspect ratios", "6", "6"),
            ("Product compositing", "✅ Composite pipeline", "✅ Yes — they were first to this approach"),
            ("REST API", "✅ Yes, public docs at /docs", "⚠️ Limited — only via Zapier integration"),
            ("Per-user image library", "✅ Yes", "✅ Yes"),
            ("Speed (1K image)", "~8s (Fast) / ~25s (Balanced)", "~30s"),
            ("Open-source", "Internal — open to white-label", "❌ Closed"),
        ],
        "verdict": """<p><strong>Pebblely</strong> is the closest direct competitor and the most
honest comparison. They were first to the "composite your product into a
generated scene" approach, and they've been doing this longer than us.</p>
<p>Pick <strong>Pebblely</strong> if you want the more mature composite pipeline
and are happy with a subscription. Pick <strong>Photogen</strong> if you want a
public REST API, BYOK pricing, or the option to self-host. Photogen is also
cheaper at the bottom tier ($0.02 vs Pebblely's $13/mo minimum).</p>""",
        "faqs": [
            ("Is Pebblely or Photogen better at compositing?", "Very close. Pebblely has been doing it longer and has a slightly more refined pipeline. Photogen's approach is competitive and uses newer models. The visual diff in head-to-head is usually a tie."),
            ("Does Pebblely have a public API?", "Only via Zapier. Photogen has a full REST API documented at <a href='/docs'>/docs</a>."),
            ("Can I import my Pebblely library into Photogen?", "Pebblely exports PNGs. Drag them into Photogen, paste a prompt, regenerate. Most teams end up keeping Pebblely library as a record and building new generations in Photogen."),
        ],
    },
    "booth": {
        "name": "Booth.AI",
        "url": "https://booth.ai",
        "tagline": "Product photography for ecommerce, prompt-based",
        "rows": [
            ("Pricing model", "Pay per image ($0.02–$0.24), BYOK free", "Subscription + per-image credits"),
            ("Bring your own API key", "✅ Yes", "❌ No"),
            ("Self-hostable", "✅ Yes", "❌ No"),
            ("Pricing floor", "$0.02/image", "$19/mo Starter"),
            ("Batch 4-up", "✅ Yes", "✅ Yes"),
            ("Aspect ratios", "6", "5"),
            ("On-model photography", "❌ Roadmap Q3 2026", "✅ Yes"),
            ("Product compositing", "✅ Yes", "✅ Yes"),
            ("REST API", "✅ Public, at /docs", "✅ Yes — but limited endpoints"),
            ("Speed", "8s (Fast) / 25s (Balanced) / 45s (Quality)", "~30s flat"),
            ("Pricing transparency", "Full per-image cost shown before generation", "Hidden behind subscription tiers"),
        ],
        "verdict": """<p><strong>Booth.AI</strong> is solid, similar feature set to Photogen and
Pebblely. The biggest differentiator is their on-model photography, which we
don't have yet.</p>
<p>Pick <strong>Booth.AI</strong> if on-model photography is critical to your
workflow today. Pick <strong>Photogen</strong> if you want transparent per-image
pricing, BYOK, a real REST API, or self-hosting.</p>""",
        "faqs": [
            ("Does Booth.AI work with custom backdrops?", "Yes, both products do. Drop a background reference or describe it in the prompt."),
            ("Is Booth.AI or Photogen better for Amazon PDP?", "Both are fine. Amazon's image spec (1000x1000 minimum, white background for the main image) is supported in both. Photogen's Composite pipeline ensures your label is exact, which is the bigger differentiator."),
            ("Does Photogen have on-model photography?", "Roadmap Q3 2026. Booth.AI has it today."),
        ],
    },
    "midjourney": {
        "name": "Midjourney",
        "url": "https://midjourney.com",
        "tagline": "General-purpose AI image generation, Discord-first",
        "rows": [
            ("Pricing model", "Pay per image ($0.02–$0.24), BYOK free", "Subscription $10–$120/mo"),
            ("Bring your own API key", "✅ Yes — Google Gemini", "❌ Closed model, no API key"),
            ("Self-hostable", "✅ Yes", "❌ No"),
            ("Product compositing", "✅ Composite pipeline — your PNG is composited in", "❌ No — generates from prompt only, hallucinates everything"),
            ("Label accuracy on packaging", "✅ High — your real PNG is used", "❌ Low — invents fake label text"),
            ("Per-image cost floor", "$0.02", "$0.10+ equivalent on Standard plan"),
            ("REST API", "✅ Public, /docs", "❌ Discord bot only"),
            ("Aspect ratios", "6", "5 (no 4:3)"),
            ("Batch 4-up", "✅ Yes", "✅ Yes (via /imagine)"),
            ("Best for", "Product photography with real packaging", "Art, illustration, moody creative"),
            ("Ecommerce-ready out of the box", "✅ Yes — clean product on clean background", "❌ Needs cleanup — text, logo, alignment often wrong"),
        ],
        "verdict": """<p>These are different products. <strong>Midjourney</strong> is a general-purpose
art generator. <strong>Photogen</strong> is purpose-built for product photography
where the actual product matters.</p>
<p>If you're making mood boards, concept art, or illustrations: Midjourney.</p>
<p>If you're shipping real product where the label, logo, and colorway must
match the SKU: Photogen. We use your actual PNG and composite it into the
generated scene, so the product is exact.</p>""",
        "faqs": [
            ("Can Midjourney do product photography?", "It can generate product-looking images, but it will invent fake label text, fake ingredient lists, and fake brand names. For real CPG/DTC work where the packaging must match, this is a deal-breaker."),
            ("Is Photogen cheaper than Midjourney?", "Yes, at the bottom tier. Photogen Fast is $0.02/image. Midjourney Standard is $0.10/image equivalent at $10/mo for 200 images."),
            ("Does Midjourney have a REST API?", "No. Photogen does, at <a href='/docs'>/docs</a>."),
        ],
    },
}
