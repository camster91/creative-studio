import pytest

from creative_studio_app.delivery import QC_CRITERIA, QC_RUBRIC_VERSION
from creative_studio_app.qc_calibration import calibrate


def assessment(model, statuses):
    return {
        "rubric_version": QC_RUBRIC_VERSION,
        "model": model,
        "criteria": {name: {"status": statuses.get(name, "unknown")} for name in QC_CRITERIA},
    }


def record(identifier, human, provider, *, second_review=None, model="pinned-model-v1"):
    reviews = [{"reviewer_id": "reviewer-a", "criteria": human}]
    if second_review is not None:
        reviews.append({"reviewer_id": "reviewer-b", "criteria": second_review})
    return {
        "fixture_id": identifier,
        "license_or_origin": "repository-owned synthetic fixture",
        "reviews": reviews,
        "assessment": assessment(model, provider),
    }


def test_calibration_measures_false_results_unknowns_and_disagreement():
    criteria = list(QC_CRITERIA)
    target = criteria[0]
    records = [
        record("correct-pass", {target: "pass"}, {target: "pass"}, second_review={target: "pass"}),
        record("false-fail", {target: "pass"}, {target: "fail"}, second_review={target: "pass"}),
        record("correct-fail", {target: "fail"}, {target: "fail"}, second_review={target: "fail"}),
        record("false-pass", {target: "fail"}, {target: "pass"}, second_review={target: "fail"}),
        record("unknown", {target: "fail"}, {target: "unknown"}, second_review={target: "fail"}),
        record("tie", {target: "pass"}, {target: "pass"}, second_review={target: "fail"}),
    ]
    report = calibrate(records)
    result = report["groups"][0]["criteria"][target]
    assert result["fixtures"] == 6
    assert result["consensus_count"] == 5
    assert result["no_consensus_count"] == 1
    assert result["reviewer_disagreement"] == {"numerator": 1, "denominator": 6, "rate": 0.1667}
    assert result["provider_coverage"] == {"numerator": 4, "denominator": 5, "rate": 0.8}
    assert result["known_accuracy"] == {"numerator": 2, "denominator": 4, "rate": 0.5}
    assert result["false_pass_rate"] == {"numerator": 1, "denominator": 3, "rate": 0.3333}
    assert result["false_fail_rate"] == {"numerator": 1, "denominator": 2, "rate": 0.5}


def test_results_are_separated_by_exact_model_and_rubric():
    target = next(iter(QC_CRITERIA))
    records = [
        record("one", {target: "pass"}, {target: "pass"}, model="model-a"),
        record("two", {target: "pass"}, {target: "pass"}, model="model-b"),
    ]
    report = calibrate(records)
    assert [(group["rubric_version"], group["model"]) for group in report["groups"]] == [
        (QC_RUBRIC_VERSION, "model-a"), (QC_RUBRIC_VERSION, "model-b")
    ]


@pytest.mark.parametrize("mutation,message", [
    ({"fixture_id": "", "license_or_origin": "owned", "reviews": [], "assessment": {}}, "fixture_id"),
    ({"fixture_id": "x", "license_or_origin": "", "reviews": [], "assessment": {}}, "license_or_origin"),
    ({"fixture_id": "x", "license_or_origin": "owned", "reviews": [], "assessment": {}}, "human review"),
])
def test_calibration_refuses_untraceable_or_unreviewed_fixtures(mutation, message):
    with pytest.raises(ValueError, match=message):
        calibrate([mutation])


def test_duplicate_fixture_ids_are_rejected():
    target = next(iter(QC_CRITERIA))
    item = record("same", {target: "pass"}, {target: "pass"})
    with pytest.raises(ValueError, match="Duplicate"):
        calibrate([item, item])


def test_duplicate_or_anonymous_reviewers_cannot_count_as_independent():
    target = next(iter(QC_CRITERIA))
    item = record("same-reviewer", {target: "pass"}, {target: "pass"}, second_review={target: "pass"})
    item["reviews"][1]["reviewer_id"] = "reviewer-a"
    with pytest.raises(ValueError, match="unique"):
        calibrate([item])
    item["reviews"][1]["reviewer_id"] = ""
    with pytest.raises(ValueError, match="reviewer_id"):
        calibrate([item])
