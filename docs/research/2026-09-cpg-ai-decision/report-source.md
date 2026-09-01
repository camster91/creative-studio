# CPG AI implementation decision refresh

Audience: Creative Studio product and engineering

Date: 2026-09-01

## Scope and assumptions

This refresh asks which capability should be implemented next for a North
American, human-controlled CPG Campaign Factory. It assumes lean and mid-market
brand teams, image-led campaigns, United States and Canadian launch markets,
and no claim that software provides legal approval. It excludes autonomous ad
publishing, medical diagnosis, and unsupported market-size estimates.

## Executive answer

Do not make OpenAI model integration the next production dependency. The current
OpenAI platform can support multimodal brief intake, strict structured work orders,
image generation/editing, asynchronous batches, and image-aware evals. Those
capabilities make a non-chat Go-button workflow feasible, but they do not solve
the product's most consequential current gap: approved claims are still stored as
unversioned strings without market, channel, substantiation, disclosure, expiry,
or accountable approval evidence.

Implement a first-class approved-claim registry next. Keep deterministic pack
compositing, channel recipes, and exception gating as the production shell. Add
OpenAI only behind a provider contract after the licensed evaluation corpus can
compare it to the existing provider on accuracy, correction time, latency, and
cost.

## Evidence and implications

OpenAI Structured Outputs can enforce a supported subset of JSON Schema, so it is
appropriate for compiling a messy brief into a typed draft—not for independently
authorizing product truth. The Responses platform accepts image inputs and tools;
GPT Image supports generation/editing, transparent output, streaming partials,
and usage accounting. The Batch API supports asynchronous jobs, and OpenAI Evals
can use text and image inputs with deterministic or model-based graders.

These capabilities support the planned architecture, but current data-control
behavior must be assessed per endpoint. OpenAI documents GPT Image endpoints as
Zero Data Retention compatible while files and stateful objects have their own
deletion and retention behavior. The application must therefore keep its own
provider-independent work order, lineage, deletion contract, and cost ledger.

Claims governance cannot be delegated to a model score. FTC guidance says
advertisers must identify both express and implied claims and possess adequate
substantiation before dissemination; health claims generally require competent
and reliable scientific evidence. FDA distinguishes health, nutrient-content,
and structure/function claims and imposes different review, disclaimer, and
notification conditions. Health Canada likewise requires food health claims in
labelling and advertising to be truthful, non-misleading, and scientifically
substantiated. The product must store exact approved text, jurisdiction, channel,
claim class, supporting source, disclosure, expiry, and accountable decision.

Channel rules are placement-specific rather than one universal size table. Amazon
Ads, for example, prohibits pricing claims and most added text in responsive custom
images and warns against poor cropping, while Shopify creates theme-dependent image
variants and accepts broad source dimensions. Therefore a recipe must identify its
placement and rule version; exact pixel dimensions alone are insufficient evidence
of compliance.

## Decision

1. Implement `ApprovedClaim` as an owner-scoped, immutable approval record.
2. Require an HTTPS substantiation reference and reasoned accountable approval.
3. Scope every claim to market and channel and preserve required disclosure and
   expiry.
4. Compile only active, applicable claim records into a campaign plan.
5. Route missing, expired, or mismatched claim evidence to readiness or exception
   gates; never have a model silently repair legal meaning.
6. Build an OpenAI provider adapter only after the existing calibration contract
   has licensed, independently reviewed cases and promotion thresholds.

## Material limitations

This research does not establish legal advice, marketplace certification, buyer
demand, or provider superiority. Amazon policies vary by ad product and seller
listing context. Shopify rendering varies by theme. No licensed CPG corpus or
target-customer interviews were available. Pricing and model aliases are volatile
and must be checked again when provider spend is authorized.

## Sources

- [OpenAI Responses and Structured Outputs API](https://platform.openai.com/docs/api-reference/responses)
- [OpenAI Images API](https://developers.openai.com/api/reference/resources/images)
- [OpenAI Batch API](https://developers.openai.com/api/reference/resources/batches)
- [OpenAI Evals API](https://developers.openai.com/api/reference/resources/evals)
- [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data)
- [FTC Health Products Compliance Guidance](https://www.ftc.gov/business-guidance/resources/health-products-compliance-guidance)
- [FDA label claims for conventional foods and dietary supplements](https://www.fda.gov/food/nutrition-food-labeling-and-critical-foods/label-claims-conventional-foods-and-dietary-supplements)
- [Health Canada health-claim assessments](https://www.canada.ca/en/health-canada/services/food-nutrition/food-labelling/health-claims/assessments.html)
- [Amazon Ads eCommerce creative specifications](https://advertising.amazon.com/resources/ad-specs/ecommerce)
- [Shopify product media requirements](https://help.shopify.com/en/manual/products/product-media/product-media-types)

## Stop condition

Research stopped after the API feasibility, privacy, claims-governance, channel,
and sequencing decisions had primary support and another broad vendor search was
unlikely to change the implementation priority.
