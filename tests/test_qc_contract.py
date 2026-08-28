import json
from pathlib import Path

from creative_studio_app.delivery import (
    QC_RUBRIC_VERSION,
    normalize_qc_assessment,
    parse_qc_output,
    run_qc,
)


def test_structured_assessment_preserves_evidence_and_uncertainty():
    result = normalize_qc_assessment(
        {
            "quality_score": 8,
            "criteria": {
                name: {
                    "status": "pass",
                    "evidence": f"visible evidence for {name}",
                    "confidence": "high",
                }
                for name in (
                    "physical_grounding", "text_integrity", "shadow_attachment",
                    "product_authenticity", "label_readability",
                )
            },
            "issues": [],
        },
        model="test-model-v1",
    )
    assert result["rubric_version"] == QC_RUBRIC_VERSION
    assert result["model"] == "test-model-v1"
    assert result["confidence"] == "high"
    assert result["advisory"] is True
    assert all(item["passed"] for item in result["criteria"].values())
    assert any("never blocked" in limitation for limitation in result["limitations"])


def test_unknown_or_invalid_provider_values_do_not_become_false_certainty():
    result = normalize_qc_assessment(
        {"quality_score": 99, "criteria": {"physical_grounding": {"status": "maybe"}}},
        model="test-model",
    )
    assert result["quality_score"] is None
    assert result["confidence"] == "low"
    assert all(item["status"] == "unknown" for item in result["criteria"].values())


def test_machine_marker_round_trips_without_text_scraping():
    expected = normalize_qc_assessment({}, model="test-model")
    output = "human diagnostics\nQC_JSON: " + json.dumps(expected) + "\nmore text"
    assert parse_qc_output(output) == expected


def test_provider_failure_is_redacted_and_still_advisory(tmp_path):
    class Failed:
        stderr = "secret prompt and provider internals"

    import subprocess

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "qc", stderr=Failed.stderr)

    result = run_qc(
        str(tmp_path / "image.png"),
        "secret-api-key",
        launch_script=Path("launch.sh"),
        run=fail,
        estimated_cost_usd=0.01,
    )
    assert result["error"] == "QC provider request failed"
    assert "secret" not in json.dumps(result)
    assert result["advisory"] is True
    assert result["estimated_cost_usd"] == 0.01
