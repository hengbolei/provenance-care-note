from uuid import uuid4

import pytest

from carenote.core.identity import AuthenticationError, session_service
from carenote.domain.models import (
    Actor,
    AgentActor,
    EntryOrigin,
    EntryRef,
    EntryType,
    Patient,
    ProvenanceSource,
    Role,
    TimelineEntry,
)
from carenote.services.care_notes import AccessDeniedError


def _patient(patient_id, clinic_id):
    return Patient(
        id=patient_id,
        clinic_id=clinic_id,
        display_name="Synthetic Patient",
        date_of_birth="1980-01-01",
        medical_record_number="MRN-TEST-1",
    )


def _entry(actor, patient_id, entry_type=EntryType.SYSTEM_EVENT):
    return TimelineEntry(
        patient_id=patient_id,
        clinic_id=actor.clinic_id,
        author_role=actor.role,
        author_id=actor.id,
        type=entry_type,
        title="Agent note",
        content="Synthetic agent output.",
        origin=EntryOrigin(
            source=ProvenanceSource.MANUAL_ENTRY, source_id="agent", source_label="Agent"
        ),
    )


def test_agent_actor_is_system_scoped_to_its_clinician(clinician):
    run_id = uuid4()
    agent = AgentActor.for_run(clinician, run_id)

    assert agent.role is Role.SYSTEM
    assert agent.clinic_id == clinician.clinic_id
    assert agent.principal == clinician
    assert agent.id == AgentActor.for_run(clinician, run_id).id
    assert agent.id not in {clinician.id, AgentActor.for_run(clinician, uuid4()).id}


@pytest.mark.parametrize("role", [Role.PATIENT, Role.STAFF, Role.ADMIN, Role.SYSTEM])
def test_only_clinicians_can_start_an_agent_run(role, clinic_id):
    with pytest.raises(PermissionError):
        AgentActor.for_run(Actor(id=uuid4(), role=role, clinic_id=clinic_id), uuid4())


def test_agent_actor_role_cannot_be_overridden(clinician):
    with pytest.raises(ValueError):
        AgentActor(
            id=uuid4(),
            role=Role.CLINICIAN,
            clinic_id=clinician.clinic_id,
            run_id=uuid4(),
            principal=clinician,
        )


def test_agent_actor_cannot_hold_an_interactive_session(clinician):
    with pytest.raises(AuthenticationError):
        session_service.issue(AgentActor.for_run(clinician, uuid4()))


def test_agent_reads_as_its_principal_but_cannot_read_as_itself(
    service, repository, clinician, patient_id, clinic_id
):
    repository.add_patient(_patient(patient_id, clinic_id))
    agent = AgentActor.for_run(clinician, uuid4())

    assert service.get_patient_view(agent.principal, patient_id).patient.id == patient_id
    with pytest.raises(AccessDeniedError):
        service.get_patient_view(agent, patient_id)
    with pytest.raises(AccessDeniedError):
        service.list_audit_events(agent, patient_id)


def test_agent_cannot_write_entries_or_sections(
    service, repository, clinician, patient_id, clinic_id
):
    repository.add_patient(_patient(patient_id, clinic_id))
    agent = AgentActor.for_run(clinician, uuid4())

    with pytest.raises(AccessDeniedError):
        service.add_entry(agent, _entry(agent, patient_id))
    with pytest.raises(AccessDeniedError):
        service.update_section(agent, patient_id, "agent_review", "Draft", expected_version=0)
    assert repository.list_entries(patient_id, clinic_id) == []
    assert repository.get_section(patient_id, "agent_review") is None


def test_clinician_can_still_create_the_agent_review_section(
    service, repository, clinician, patient_id, clinic_id
):
    repository.add_patient(_patient(patient_id, clinic_id))

    state = service.update_section(
        clinician, patient_id, "agent_review", "Accepted claims", expected_version=0
    )

    assert state.owner_role is Role.CLINICIAN
    assert state.version == 1


def test_entry_ref_records_what_verification_needs(clinician, patient_id):
    entry = _entry(clinician, patient_id, EntryType.CLINICIAN_NOTE)

    ref = EntryRef.from_entry(entry)

    assert ref.entry_id == entry.id
    assert ref.length == len(entry.content)
    assert (ref.type, ref.author_role, ref.internal) == (
        EntryType.CLINICIAN_NOTE,
        Role.CLINICIAN,
        True,
    )
