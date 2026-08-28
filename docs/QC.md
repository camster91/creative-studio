# AI quality review contract

QC is advisory. It never blocks export by itself and is not evidence of legal,
marketplace, accessibility, brand, or product-authenticity compliance.

The versioned `cpg-photo-v1` rubric evaluates five visible criteria:

| Criterion | Pass meaning | Unsupported inference |
| --- | --- | --- |
| Physical grounding | The product visibly contacts a plausible surface | Physical correctness outside the visible frame |
| Text integrity | Visible text has no obvious generative corruption | Exact legal-copy correctness |
| Shadow attachment | Visible shadows plausibly connect subject and scene | Lighting measurements or hidden light sources |
| Product authenticity | No visible invented package/product features | Verification against an unavailable source product |
| Label readability | Material label text is visibly legible at supplied resolution | Regulatory or marketplace compliance |

Each criterion returns `pass`, `fail`, or `unknown`, a concise visible-evidence
statement, and low/medium/high confidence. Missing or malformed provider fields
become `unknown`, never an inferred pass. Overall score is accepted only as an
integer from 1–10. Overall confidence is high only when all five criteria are
known and high-confidence.

Provider/model, rubric version, limitations, configured estimated cost, and
issues are returned with every result. Provider failures return a redacted,
low-confidence advisory response without stderr, prompts, keys, or image data.

Human reviewers can record an owner-scoped `accept` or `reject` override with a
required reason through `/api/qc/override`. That record is appended to the
owner's session; it does not rewrite the original AI result.

## Calibration release gate

Before claiming QC is calibrated, reviewers must label a licensed image fixture
set independently, freeze its expected criterion outcomes, run the pinned model,
and publish per-criterion false-pass, false-fail, unknown, and disagreement
rates. Thresholds must be chosen from that evidence. Until those results exist,
the UI and API must continue to call QC advisory and show its limitations.
