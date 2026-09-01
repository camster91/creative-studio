# CPG AI implementation decision — September 2026

## Decision

Creative Studio should continue becoming a governed Campaign Factory rather than
a chatbot. OpenAI is technically suitable for typed multimodal brief intake,
image generation/editing, offline batches, and evaluation, but adding a second
provider is not the next production dependency.

The immediate product gap is claim provenance. Before a CPG asset can be treated
as campaign-ready, each claim needs exact approved wording, a claim class, market
and channel scope, substantiation, required disclosure, expiry, and an accountable
decision. Models may flag or explain risk; they must not manufacture substantiation
or provide final legal approval.

## Why

- OpenAI Structured Outputs can compile a brief into a supported JSON Schema, but
  the application must still own validation, refusal handling, state, permissions,
  and audit evidence. [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
- GPT Image supports generation and editing with streaming, transparent output,
  and usage accounting, making it a viable future provider candidate—not proof of
  package or label fidelity. [OpenAI Images API](https://developers.openai.com/api/reference/resources/images)
- OpenAI offers asynchronous batch processing and image-aware evals, which fit
  offline corpus comparisons after licensed test cases exist. [OpenAI Batch API](https://developers.openai.com/api/reference/resources/batches), [OpenAI Evals API](https://developers.openai.com/api/reference/resources/evals)
- FTC guidance requires advertisers to identify express and implied claims and
  possess adequate substantiation before dissemination. [FTC Health Products Compliance Guidance](https://www.ftc.gov/business-guidance/resources/health-products-compliance-guidance)
- FDA and Health Canada distinguish claim categories and require truthful,
  non-misleading, scientifically supported claims under market-specific rules.
  [FDA label claims](https://www.fda.gov/food/nutrition-food-labeling-and-critical-foods/label-claims-conventional-foods-and-dietary-supplements), [Health Canada assessments](https://www.canada.ca/en/health-canada/services/food-nutrition/food-labelling/health-claims/assessments.html)
- Channel rules differ by placement and include content and crop constraints beyond
  dimensions. [Amazon Ads specifications](https://advertising.amazon.com/resources/ad-specs/ecommerce), [Shopify media types](https://help.shopify.com/en/manual/products/product-media/product-media-types)

## Implementation order

1. First-class approved-claim registry and fail-closed campaign readiness.
2. Multi-role approval policy and immutable claim/rule revision history.
3. Licensed, independently reviewed CPG evaluation corpus.
4. Provider-neutral OpenAI/Gemini comparison using accuracy, human correction
   time, latency, and cost per approved package.
5. Promote a provider only when predeclared thresholds pass.

## Boundary

This is a product and engineering decision, not legal advice or marketplace
certification. No licensed corpus, customer interviews, or provider comparison was
available. Model IDs, pricing, retention eligibility, and channel policies must be
rechecked at the point of provider or publishing activation.
