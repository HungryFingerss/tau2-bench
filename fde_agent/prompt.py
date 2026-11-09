"""The model's instructions, read from instructions.md (versioned in git, like code)."""

from pathlib import Path

INSTRUCTIONS_FILE = Path(__file__).with_name("instructions.md")


def load_instructions() -> str:
    return INSTRUCTIONS_FILE.read_text(encoding="utf-8").strip()
