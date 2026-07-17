import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TEXT_SUFFIXES = {".md", ".py", ".toml", ".yaml", ".yml"}
PRIVATE_MARKERS = (
    "savi" + "nien",
    "aez" + "aror",
    "cau" + "dry",
    "sk-" + "proj-",
)


def test_public_tree_contains_no_personal_markers() -> None:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in PUBLIC_TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8").lower()
        findings.extend(
            f"{path.relative_to(ROOT)}: {marker}" for marker in PRIVATE_MARKERS if marker in text
        )
    assert findings == []


def test_readme_uses_source_rendered_diagrams_only() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"!\[[^]]*]\([^)]*\.(?:png|jpe?g)\)", readme, re.IGNORECASE) is None
    assert "```mermaid" in readme
