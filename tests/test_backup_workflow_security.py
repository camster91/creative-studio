from pathlib import Path
import re


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "backup-data.yml"
FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _backup_step_blocks() -> list[list[str]]:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    steps_start = lines.index("    steps:") + 1
    blocks: list[list[str]] = []
    for line in lines[steps_start:]:
        if line.startswith("      - "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return blocks


def _step_value(block: list[str], key: str) -> str | None:
    inline = re.fullmatch(rf"      - {re.escape(key)}:\s*([^#]+?)(?:\s+#.*)?", block[0])
    if inline:
        return inline.group(1).strip()
    for line in block[1:]:
        nested = re.fullmatch(rf"        {re.escape(key)}:\s*([^#]+?)(?:\s+#.*)?", line)
        if nested:
            return nested.group(1).strip()
    return None


def test_privileged_backup_actions_are_pinned_to_reviewed_commits():
    references = [
        value.split("@", maxsplit=1)
        for block in _backup_step_blocks()
        if (value := _step_value(block, "uses")) is not None
    ]

    assert references == [
        ["actions/checkout", "3d3c42e5aac5ba805825da76410c181273ba90b1"],
        ["actions/upload-artifact", "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"],
    ]
    assert all(FULL_COMMIT_SHA.fullmatch(ref) for _, ref in references)


def test_backup_checkout_does_not_persist_repository_credentials():
    checkout_steps = [
        block
        for block in _backup_step_blocks()
        if (_step_value(block, "uses") or "").startswith("actions/checkout@")
    ]

    assert len(checkout_steps) == 1
    assert "          persist-credentials: false" in checkout_steps[0]
