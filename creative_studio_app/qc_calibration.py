"""Aggregate human-reviewed QC calibration without retaining creative payloads."""

from collections import defaultdict

from .delivery import QC_CRITERIA, QC_RUBRIC_VERSION


VALID_STATUSES = frozenset({"pass", "fail", "unknown"})


def _rate(numerator: int, denominator: int) -> dict:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


def _human_consensus(reviews: list[dict], criterion: str) -> tuple[str | None, bool]:
    labels = [review.get("criteria", {}).get(criterion) for review in reviews]
    labels = [label for label in labels if label in {"pass", "fail"}]
    if not labels:
        return None, False
    disagreement = len(set(labels)) > 1
    passing = labels.count("pass")
    failing = labels.count("fail")
    if passing == failing:
        return None, disagreement
    return ("pass" if passing > failing else "fail"), disagreement


def _validate_record(record: dict) -> None:
    if not isinstance(record, dict):
        raise ValueError("Each calibration record must be an object")
    if not str(record.get("fixture_id") or "").strip():
        raise ValueError("Each calibration record requires a fixture_id")
    if not str(record.get("license_or_origin") or "").strip():
        raise ValueError("Each calibration record requires license_or_origin evidence")
    reviews = record.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        raise ValueError("Each calibration record requires at least one human review")
    reviewers = []
    for review in reviews:
        if not isinstance(review, dict) or not str(review.get("reviewer_id") or "").strip():
            raise ValueError("Each human review requires a reviewer_id")
        if not isinstance(review.get("criteria"), dict):
            raise ValueError("Each human review requires criterion labels")
        reviewers.append(str(review["reviewer_id"]))
    if len(reviewers) != len(set(reviewers)):
        raise ValueError("Reviewer IDs must be unique within each fixture")
    assessment = record.get("assessment")
    if not isinstance(assessment, dict):
        raise ValueError("Each calibration record requires a provider assessment")
    if not str(assessment.get("model") or "").strip():
        raise ValueError("Each provider assessment requires an exact model/version")
    if not str(assessment.get("rubric_version") or "").strip():
        raise ValueError("Each provider assessment requires a rubric_version")
    if not isinstance(assessment.get("criteria"), dict):
        raise ValueError("Each provider assessment requires criterion results")


def calibrate(records: list[dict]) -> dict:
    """Return aggregate false-result, unknown, coverage, and disagreement evidence."""
    if not isinstance(records, list) or not records:
        raise ValueError("Calibration requires at least one record")
    groups = defaultdict(list)
    seen = set()
    for record in records:
        _validate_record(record)
        fixture_id = str(record["fixture_id"])
        if fixture_id in seen:
            raise ValueError(f"Duplicate fixture_id: {fixture_id}")
        seen.add(fixture_id)
        assessment = record["assessment"]
        rubric = str(assessment.get("rubric_version") or "unknown")
        model = str(assessment.get("model") or "unknown")
        groups[(rubric, model)].append(record)

    reports = []
    for (rubric, model), group in sorted(groups.items()):
        criterion_reports = {}
        for criterion in QC_CRITERIA:
            counts = defaultdict(int)
            for record in group:
                reviews = record["reviews"]
                truth, disagreement = _human_consensus(reviews, criterion)
                if len(reviews) >= 2:
                    counts["independently_reviewed"] += 1
                if disagreement:
                    counts["reviewer_disagreement"] += 1
                if truth is None:
                    counts["no_consensus"] += 1
                    continue
                counts["consensus"] += 1
                counts[f"human_{truth}"] += 1
                supplied = record["assessment"].get("criteria", {}).get(criterion, {})
                provider = supplied.get("status") if isinstance(supplied, dict) else None
                provider = provider if provider in VALID_STATUSES else "unknown"
                counts[f"provider_{provider}"] += 1
                if provider == "unknown":
                    counts["unknown"] += 1
                elif provider == truth:
                    counts["correct"] += 1
                elif provider == "pass" and truth == "fail":
                    counts["false_pass"] += 1
                elif provider == "fail" and truth == "pass":
                    counts["false_fail"] += 1
            known = counts["consensus"] - counts["unknown"]
            criterion_reports[criterion] = {
                "fixtures": len(group),
                "consensus_count": counts["consensus"],
                "no_consensus_count": counts["no_consensus"],
                "independent_review_coverage": _rate(counts["independently_reviewed"], len(group)),
                "reviewer_disagreement": _rate(counts["reviewer_disagreement"], len(group)),
                "provider_coverage": _rate(known, counts["consensus"]),
                "known_accuracy": _rate(counts["correct"], known),
                "unknown_rate": _rate(counts["unknown"], counts["consensus"]),
                "false_pass_rate": _rate(counts["false_pass"], counts["human_fail"]),
                "false_fail_rate": _rate(counts["false_fail"], counts["human_pass"]),
            }
        reports.append({
            "rubric_version": rubric,
            "model": model,
            "fixture_count": len(group),
            "criteria": criterion_reports,
        })
    return {
        "schema_version": 1,
        "advisory": True,
        "expected_rubric_version": QC_RUBRIC_VERSION,
        "record_count": len(records),
        "groups": reports,
        "limitations": [
            "Calibration quality depends on licensed representative fixtures and independent human review.",
            "Small samples and reviewer disagreement must not be presented as proof of marketplace compliance.",
            "Results apply only to the recorded rubric and pinned provider model/version.",
        ],
    }
