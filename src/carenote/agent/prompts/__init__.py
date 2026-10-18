"""Versioned prompt templates. A prompt's version is stored on every run and keys its cassettes."""

from pathlib import Path

_DIR = Path(__file__).parent


def load_prompt(version: str) -> str:
    # Normalise newlines so a Windows checkout (CRLF) and CI (LF) send identical prompts and
    # therefore match the same recorded cassettes.
    return (_DIR / f"{version}.md").read_text(encoding="utf-8").replace("\r\n", "\n")
