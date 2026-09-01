# QC calibration protocol

The `cpg-photo-v1` assessment is advisory. It becomes measurable only when the
same licensed or organization-owned fixture is independently reviewed by
people and assessed by an exact pinned provider model/version.

## Record contract

Store calibration inputs outside production customer data. Each JSON record
must include:

- a non-customer `fixture_id`;
- `license_or_origin` evidence;
- one or preferably two independent human `reviews`, each containing
  `criteria` statuses of `pass` or `fail`;
- the complete normalized provider `assessment`, including exact `model`,
  `rubric_version`, and criterion statuses.

Do not put prompts, image bytes, customer names, email addresses, API keys, or
tokens in the calibration record. Keep the licensed image corpus in its
approved access-controlled location.

## Run

```bash
.venv/bin/python scripts/calibrate-qc.py calibration-input.json --output calibration-report.json
```

The aggregate report separates every rubric/model pair and reports, per
criterion:

- independent-review coverage and reviewer disagreement;
- provider coverage and unknown rate;
- accuracy among known results;
- false-pass rate among human-consensus failures;
- false-fail rate among human-consensus passes.

The report deliberately contains aggregate counts rather than fixture IDs or
creative payloads.

## Review gate

Before using results in a release or commercial claim:

1. Confirm every fixture has recorded license/origin and represents the target
   CPG workflow.
2. Use two independent reviewers and adjudicate disagreements without changing
   their original labels.
3. Pin and record the provider model/version; never combine model versions.
4. Predeclare minimum sample sizes and acceptable false-pass, false-fail,
   unknown, and disagreement thresholds for each criterion.
5. Review the aggregate report with product, creative, and compliance owners.
6. Keep export advisory: an opaque score alone must never block delivery.

Repository tests prove the aggregation math and privacy shape. They do not
constitute a reviewed fixture corpus or provider calibration result.
