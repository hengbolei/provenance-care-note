# Agent evaluation

Golden cases for the Clinical Review Agent (design §13), and the cassettes the agent replays.

```
evals/
  cases/       # one YAML file per family; each holds a list of cases
  cassettes/   # recorded model responses, keyed by request fingerprint
```

The schema and loader are in `src/carenote/evals/cases.py`.

## A case

```yaml
- id: dose-03-warfarin-decimal          # lower-case, unique
  family: dose_conflict                 # one of the eight families below
  description: A decimal dose reduction that the patient has not followed.
  patient:                              # optional; these values are redacted
    display_name: Example Patient (Synthetic)
    medical_record_number: SYN-EVAL-0001
  entries:                              # the synthetic record
    - key: reduce
      type: clinician_note              # any timeline entry type
      author: clinician                 # the authoring role
      at: 2026-08-14T16:00:00Z
      title: INR high
      content: INR 3.8. Reduce warfarin 2.5 mg nightly and recheck INR on Friday.
  filler:                               # optional repeated entries for long timelines
    - {count: 55, type: staff_note, author: staff, start: 2026-06-02T08:00:00Z,
       title: Routine observation {n}, content: Routine observation {n}.}
  expect:
    conflicts: [{category: dose, entity: warfarin}]   # what conflict detection must find
    phi: [Example Patient (Synthetic)]                # what redaction must remove
    critical_facts:                                   # what a good review must state
      - {key: reduced_dose, description: Warfarin reduced, mentions: [warfarin, 2.5 mg]}
    forbidden_output: []                              # text a draft must never contain
  probes:                               # claims and the verdict the verifier must reach
    - id: wrong-dose
      claim: Warfarin 5 mg nightly.
      cite: [{entry: reduce, quote: Reduce warfarin 2.5 mg nightly}]
      expect: [unsupported_entities]    # empty means the claim must verify
```

A probe cites its entries by `quote`. When the quote is deliberately not in the entry, `at`
(text whose position to use) or `start` (an offset) places the citation.

### Families

`medication_change`, `dose_conflict`, `new_allergy`, `contradictory_sources`,
`sparse_information`, `long_timeline`, `prompt_injection`, `phi_heavy`.

### Known gaps

Any expected conflict, identifier or probe that the current system is known to get wrong carries
a `known_gap` with the reason. Reports list known gaps separately so they stay visible, and they
do not fail the CI gate. Remove the `known_gap` once the system handles the case; the suite then
fails if it regresses.
