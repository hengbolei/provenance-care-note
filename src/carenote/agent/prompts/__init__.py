"""Versioned prompt templates. A prompt's version is stored on every run and keys its cassettes."""

from pathlib import Path

_DIR = Path(__file__).parent


def load_prompt(version: str) -> str:
    return (_DIR / f"{version}.md").read_text(encoding="utf-8")
