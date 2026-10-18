"""The interface ships in English, Simplified Chinese and Traditional Chinese.

These checks keep the three catalogs in step: every key the page and scripts use must exist,
and every language must translate every key.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from carenote.main import app

STATIC = Path(__file__).resolve().parents[1] / "src" / "carenote" / "static"
CATALOG = (STATIC / "i18n.js").read_text(encoding="utf-8")
LANGUAGES = {"en": "en", "zhHans": "zh-Hans", "zhHant": "zh-Hant"}

# Keys the scripts build from server values, e.g. t(`status.${value}`).
DYNAMIC_KEYS = {
    "status": ["ai_suggested", "clinician_confirmed", "needs_review", "rejected"],
    "entryType": [
        "patient_note",
        "staff_note",
        "clinician_note",
        "ai_doctor_consult_summary",
        "ai_nurse_consult_summary",
        "ai_patient_session_summary",
        "system_event",
    ],
    "role": ["patient", "staff", "clinician", "admin", "system", "unassigned"],
    "action": ["open", "in_progress", "completed"],
    "risk": ["low", "moderate", "high", "critical"],
    "conflictStatus": ["needs_review", "clinician_precedence", "clinician_confirmed", "resolved"],
    "conflictCategory": ["medication", "dose", "allergy"],
    "state": [
        f"{state}.{part}"
        for state in ("loading", "empty", "forbidden", "notFound", "error")
        for part in ("title", "message")
    ],
    "agent.status": [
        "running",
        "awaiting_review",
        "committed",
        "discarded",
        "budget_exceeded",
        "failed",
    ],
    "agent.failure": [
        "unknown_entry",
        "out_of_bounds",
        "quote_mismatch",
        "unsupported_entities",
        "patient_source_risk",
        "patient_source_phrasing",
    ],
    "agent.category": ["medication", "allergy", "symptom", "plan", "follow_up", "other"],
    "agent.decision": ["accept", "edit", "reject", "skip"],
    "agent.node": [
        "load_context",
        "check_conflicts",
        "draft",
        "verify",
        "revise",
        "commit",
        "discard",
        "stop",
    ],
}


def _catalog(name):
    start = CATALOG.index(f"const {name} = {{")
    end = CATALOG.index("\n};", start)
    block = CATALOG[start:end]
    return dict(re.findall(r'^  "([\w.]+)":\s*\n?\s*(.+?),?$', block, re.MULTILINE))


CATALOGS = {name: _catalog(name) for name in LANGUAGES}


def _used_keys():
    scripts = "".join((STATIC / name).read_text(encoding="utf-8") for name in ("app.js", "api.js"))
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r'\bt\("([\w.]+)"', scripts))
    used |= set(re.findall(r'data-i18n(?:-[\w-]+)?="([\w.]+)"', html))
    used |= {f"{prefix}.{value}" for prefix, values in DYNAMIC_KEYS.items() for value in values}
    return used


@pytest.mark.parametrize("name", ["zhHans", "zhHant"])
def test_every_language_translates_every_key(name):
    assert set(CATALOGS[name]) == set(CATALOGS["en"])


def test_every_key_the_interface_uses_is_defined():
    assert _used_keys() - set(CATALOGS["en"]) == set()


def test_no_key_is_left_unused():
    assert set(CATALOGS["en"]) - _used_keys() == set()


@pytest.mark.parametrize("name", ["zhHans", "zhHant"])
def test_chinese_catalogs_are_actually_translated(name):
    # Keys whose text is a product name or code may legitimately match English.
    same_as_english = {key for key, value in CATALOGS[name].items() if value == CATALOGS["en"][key]}
    assert same_as_english <= {"plan.revisionMeta", "agent.traceId"}


@pytest.mark.parametrize("name", ["zhHans", "zhHant"])
def test_placeholders_survive_translation(name):
    placeholder = re.compile(r"\{(\w+)\}")
    for key, english in CATALOGS["en"].items():
        assert set(placeholder.findall(CATALOGS[name][key])) <= set(placeholder.findall(english)), (
            key
        )


def test_language_selector_and_catalog_are_served():
    client = TestClient(app)
    page = client.get("/").text
    catalog = client.get("/static/i18n.js")

    assert 'id="language-select"' in page
    assert catalog.status_code == 200
    for entry in (
        'en: { label: "English"',
        '"zh-Hans": { label: "简体中文"',
        '"zh-Hant": { label: "繁體中文"',
    ):
        assert entry in catalog.text
