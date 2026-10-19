"""Medication and clinical-term synonyms for query matching.

Each group is one concept and its first word is the canonical name: brand names, spelling
variants, abbreviations and word forms fold into it, in queries and passages alike, so a search
for "metformin" finds "Glucophage" and the other way round. Words are matched one at a time;
multi-word lay terms such as "blood thinner" are not mapped.

`CLASSES` is one-way: a query for the class also finds its members at a lower weight, but a query
for a member does not find the class or the other members.
"""

GROUPS: tuple[tuple[str, ...], ...] = (
    # Medications: generic name first, then brand names and spelling variants.
    ("amlodipine", "norvasc", "istin"),
    ("amoxicillin", "amoxycillin", "amoxil"),
    ("apixaban", "eliquis"),
    ("aspirin", "disprin"),
    ("atorvastatin", "lipitor"),
    ("cefalexin", "cephalexin", "keflex"),
    ("clopidogrel", "plavix"),
    ("digoxin", "lanoxin"),
    ("esomeprazole", "nexium"),
    ("furosemide", "frusemide", "lasix"),
    ("ibuprofen", "advil", "nurofen", "brufen"),
    ("levothyroxine", "thyroxine", "synthroid", "eltroxin"),
    ("lisinopril", "zestril", "prinivil"),
    ("losartan", "cozaar"),
    ("metformin", "glucophage"),
    ("omeprazole", "losec", "prilosec"),
    ("paracetamol", "acetaminophen", "tylenol", "panadol"),
    ("rivaroxaban", "xarelto"),
    ("rosuvastatin", "crestor"),
    ("salbutamol", "albuterol", "ventolin"),
    ("simvastatin", "zocor"),
    ("warfarin", "coumadin", "marevan"),
    # Clinical terms: British and American spellings, abbreviations and word forms.
    ("allergy", "allergic", "allergies"),
    ("anaemia", "anemia", "anaemic", "anemic"),
    ("breathlessness", "breathless", "dyspnoea", "dyspnea", "sob"),
    ("diarrhoea", "diarrhea"),
    ("dizziness", "dizzy", "lightheaded", "lightheadedness"),
    ("haemoglobin", "hemoglobin", "hb"),
    ("hypertension", "htn"),
    ("kidney", "kidneys", "renal"),
    ("oedema", "edema"),
    ("rash", "rashes", "hives", "urticaria"),
    ("swelling", "swollen"),
    ("vomiting", "vomit", "vomited", "emesis"),
)

CLASSES: dict[str, tuple[str, ...]] = {
    "analgesic": ("paracetamol", "ibuprofen"),
    "antibiotic": ("amoxicillin", "cefalexin", "penicillin"),
    "anticoagulant": ("apixaban", "rivaroxaban", "warfarin"),
    "antiplatelet": ("aspirin", "clopidogrel"),
    "diuretic": ("furosemide",),
    "penicillin": ("amoxicillin",),
    "statin": ("atorvastatin", "rosuvastatin", "simvastatin"),
}

CLASS_MEMBER_WEIGHT = 0.7

_CANONICAL = {word: group[0] for group in GROUPS for word in group}


def _stem(token: str) -> str:
    """Fold a plain plural ("observations") onto its singular; leaves "-ss", "-us", "-is" alone."""
    plural = token.endswith("s") and not token.endswith(("ss", "us", "is"))
    return token[:-1] if plural and token.isalpha() and len(token) > 3 else token


def concept(token: str) -> str:
    """The canonical concept for a lower-case token."""
    if token in _CANONICAL:
        return _CANONICAL[token]
    stemmed = _stem(token)
    return _CANONICAL.get(stemmed, stemmed)


def expand(term: str) -> list[tuple[str, float]]:
    """A query concept and, for a drug class, its members, each with a match weight."""
    return [(term, 1.0)] + [(member, CLASS_MEMBER_WEIGHT) for member in CLASSES.get(term, ())]
