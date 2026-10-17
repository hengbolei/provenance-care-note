import {
  ApiError,
  DEMO_CONTEXT,
  addComment,
  addTimelineEntry,
  compareSectionRevisions,
  createHighlight,
  getAuditEvents,
  getEntrySource,
  getCommentRevisions,
  getHighlightRevisions,
  getHighlightSource,
  getConflictRevisions,
  getPatientDetail,
  getSectionRevisions,
  getSession,
  ingestAI,
  login,
  logout,
  recordHighlightImpression,
  revertSection,
  setCommentResolved,
  transcribeAudio,
  updateHighlight,
  updateConflict,
  updateSection,
} from "/static/api.js";

const elements = {
  name: document.querySelector("#patient-name"),
  pronouns: document.querySelector("#patient-pronouns"),
  mrn: document.querySelector("#patient-mrn"),
  dob: document.querySelector("#patient-dob"),
  content: document.querySelector("#patient-content"),
  highlightList: document.querySelector("#highlight-list"),
  highlightCount: document.querySelector("#highlight-count"),
  conflictPanel: document.querySelector("#conflict-panel"),
  conflictList: document.querySelector("#conflict-list"),
  conflictCount: document.querySelector("#conflict-count"),
  timelineList: document.querySelector("#timeline-list"),
  entryCount: document.querySelector("#entry-count"),
  patientHeader: document.querySelector("#patient-header"),
  screenState: document.querySelector("#screen-state"),
  stateTitle: document.querySelector("#state-title"),
  stateMessage: document.querySelector("#state-message"),
  retryButton: document.querySelector("#retry-button"),
  sectionContent: document.querySelector("#section-content"),
  sectionVersion: document.querySelector("#section-version"),
  saveSection: document.querySelector("#save-section"),
  showHistory: document.querySelector("#show-history"),
  sectionFeedback: document.querySelector("#section-feedback"),
  revisionList: document.querySelector("#revision-list"),
  compareFrom: document.querySelector("#compare-from"),
  compareTo: document.querySelector("#compare-to"),
  compareVersions: document.querySelector("#compare-versions"),
  versionCompareOutput: document.querySelector("#version-compare-output"),
  commentContent: document.querySelector("#comment-content"),
  commentAssignee: document.querySelector("#comment-assignee"),
  commentTarget: document.querySelector("#comment-target"),
  commentOnPlanSelection: document.querySelector("#comment-on-plan-selection"),
  addComment: document.querySelector("#add-comment"),
  commentFeedback: document.querySelector("#comment-feedback"),
  commentList: document.querySelector("#comment-list"),
  commentCount: document.querySelector("#comment-count"),
  staffNoteCard: document.querySelector("#staff-note-card"),
  entryTitle: document.querySelector("#entry-title"),
  entryContent: document.querySelector("#entry-content"),
  addEntry: document.querySelector("#add-entry"),
  entryFeedback: document.querySelector("#entry-feedback"),
  highlightComposerCard: document.querySelector("#highlight-composer-card"),
  selectedSource: document.querySelector("#selected-source"),
  highlightReason: document.querySelector("#highlight-reason"),
  highlightAction: document.querySelector("#highlight-action"),
  highlightPatientInstruction: document.querySelector("#highlight-patient-instruction"),
  highlightRiskLevel: document.querySelector("#highlight-risk-level"),
  highlightEntities: document.querySelector("#highlight-entities"),
  highlightStatus: document.querySelector("#highlight-status"),
  highlightAssignee: document.querySelector("#highlight-assignee"),
  createHighlight: document.querySelector("#create-highlight"),
  highlightCreateFeedback: document.querySelector("#highlight-create-feedback"),
  auditList: document.querySelector("#audit-list"),
  auditCount: document.querySelector("#audit-count"),
  patientDashboard: document.querySelector("#patient-dashboard"),
  patientActionList: document.querySelector("#patient-action-list"),
  patientActionCount: document.querySelector("#patient-action-count"),
  patientEntryTitle: document.querySelector("#patient-entry-title"),
  patientEntryContent: document.querySelector("#patient-entry-content"),
  addPatientEntry: document.querySelector("#add-patient-entry"),
  patientEntryFeedback: document.querySelector("#patient-entry-feedback"),
  loginPanel: document.querySelector("#login-panel"),
  loginUsername: document.querySelector("#login-username"),
  loginPassword: document.querySelector("#login-password"),
  loginButton: document.querySelector("#login-button"),
  loginFeedback: document.querySelector("#login-feedback"),
  currentRole: document.querySelector("#current-role"),
  logoutButton: document.querySelector("#logout-button"),
  glancePanel: document.querySelector("#glance-panel"),
  workspacePanel: document.querySelector("#workspace-panel"),
  presenceStatus: document.querySelector("#presence-status"),
  aiIngestCard: document.querySelector("#ai-ingest-card"),
  aiTitle: document.querySelector("#ai-title"),
  aiSource: document.querySelector("#ai-source"),
  aiRawText: document.querySelector("#ai-raw-text"),
  aiIngest: document.querySelector("#ai-ingest"),
  recordAudio: document.querySelector("#record-audio"),
  aiFeedback: document.querySelector("#ai-feedback"),
};

let currentPlanVersion = 0;
let replyToCommentId = null;
let selectedHighlightSource = null;
let selectedCommentSpanTarget = null;
let realtimeSocket = null;
let mediaRecorder = null;
let audioChunks = [];
const recordedImpressions = new Set();

const screenStates = {
  loading: ["Loading longitudinal record", "Securely retrieving patient details, priorities, and timeline."],
  empty: ["No longitudinal record yet", "This patient has no visible priority items or timeline entries."],
  forbidden: ["Access denied", "Your current identity cannot view this patient record. Check your role and clinic scope."],
  notFound: ["Patient record not found", "This patient does not exist or is not visible within your clinic scope."],
  error: ["Unable to load the record", "The service could not be reached. Please try again."],
};

const statusLabels = {
  ai_suggested: "AI suggested · Unconfirmed",
  clinician_confirmed: "Clinician confirmed",
  needs_review: "Needs review",
  rejected: "Rejected",
};

const entryTypeLabels = {
  patient_note: "Patient note",
  staff_note: "Staff note",
  clinician_note: "Clinician note",
  ai_doctor_consult_summary: "AI doctor consult summary",
  ai_nurse_consult_summary: "AI nurse consult summary",
  ai_patient_session_summary: "AI patient session summary",
  system_event: "System event",
};

const roleLabels = {
  patient: "Patient",
  staff: "Staff",
  clinician: "Clinician",
  admin: "Admin",
  system: "AI / System",
};

const actionStatusLabels = {
  open: "Open",
  in_progress: "In progress",
  completed: "Completed",
};

function createElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setScreenState(state) {
  if (state === "ready") {
    elements.screenState.hidden = true;
    elements.content.hidden = false;
    elements.patientHeader.setAttribute("aria-busy", "false");
    return;
  }
  const [title, message] = screenStates[state];
  elements.screenState.hidden = false;
  elements.screenState.className = `screen-state ${state}`;
  elements.screenState.dataset.state = state;
  elements.stateTitle.textContent = title;
  elements.stateMessage.textContent = message;
  elements.retryButton.hidden = !["error", "notFound"].includes(state);
  elements.content.hidden = true;
  elements.patientHeader.setAttribute("aria-busy", String(state === "loading"));
}

function showAuthenticatedSession(session) {
  DEMO_CONTEXT.actorRole = session.actor.role;
  elements.loginPanel.hidden = true;
  elements.patientHeader.hidden = false;
  elements.currentRole.textContent = roleLabels[session.actor.role] || session.actor.role;
  elements.logoutButton.hidden = false;
  connectRealtime();
}

function showLogin() {
  DEMO_CONTEXT.actorRole = null;
  elements.loginPanel.hidden = false;
  elements.patientHeader.hidden = true;
  elements.screenState.hidden = true;
  elements.content.hidden = true;
  elements.currentRole.textContent = "Signed out";
  elements.logoutButton.hidden = true;
  elements.presenceStatus.hidden = true;
  realtimeSocket?.close();
  realtimeSocket = null;
  elements.loginPassword.value = "";
}

function clearPatient() {
  elements.name.textContent = "Patient record";
  elements.pronouns.textContent = "—";
  elements.mrn.textContent = "—";
  elements.dob.textContent = "—";
  elements.highlightList.replaceChildren();
  elements.conflictList.replaceChildren();
  elements.timelineList.replaceChildren();
}

function formatDate(value) {
  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(`${value}T00:00:00`));
}

function formatDateTime(value) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function renderPatient(patient) {
  elements.name.textContent = patient.display_name;
  elements.pronouns.textContent = patient.pronouns || "Pronouns not provided";
  elements.mrn.textContent = patient.medical_record_number;
  elements.dob.textContent = formatDate(patient.date_of_birth);
}

function renderPatientActions(actions) {
  elements.patientActionList.replaceChildren();
  elements.patientActionCount.textContent = `${actions.length} confirmed item${actions.length === 1 ? "" : "s"}`;
  if (actions.length === 0) {
    elements.patientActionList.append(
      createElement(
        "p",
        "helper-text",
        "Your care team has not published any new guidance yet.",
      ),
    );
    return;
  }
  actions.forEach((action) => {
    const item = createElement("article", `patient-action ${action.action_status}`);
    item.append(
      createElement("p", "patient-action-title", action.title),
      createElement("p", "patient-action-instruction", action.instruction),
      createElement(
        "span",
        "patient-action-status",
        actionStatusLabels[action.action_status] || action.action_status,
      ),
    );
    elements.patientActionList.append(item);
  });
}

function focusTimelineEntries(entryIds) {
  const target = entryIds
    .map((entryId) => document.querySelector(`#entry-${entryId}`))
    .find(Boolean);
  if (!target) return;
  document.querySelectorAll(".source-target").forEach((node) => {
    node.classList.remove("source-target");
  });
  entryIds.forEach((entryId) => {
    document.querySelector(`#entry-${entryId}`)?.classList.add("source-target");
  });
  target.scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderConflicts(conflicts) {
  elements.conflictList.replaceChildren();
  elements.conflictPanel.hidden = conflicts.length === 0;
  elements.conflictCount.textContent = `${conflicts.length} conflict${conflicts.length === 1 ? "" : "s"}`;
  conflicts.forEach((conflict) => {
    const card = createElement("article", `conflict-card ${conflict.status}`);
    const top = createElement("div", "conflict-card-top");
    const conflictStatusLabels = {
      needs_review: "Needs review",
      clinician_precedence: "Clinician precedence",
      clinician_confirmed: "Clinician confirmed",
      resolved: "Resolved",
    };
    top.append(
      createElement("span", "conflict-category", conflict.category),
      createElement(
        "span",
        `status-chip ${conflict.status}`,
        conflictStatusLabels[conflict.status] || conflict.status,
      ),
    );
    const source = createElement("button", "text-button", "Compare source entries");
    source.type = "button";
    source.addEventListener("click", () => focusTimelineEntries(conflict.entry_ids));
    card.append(
      top,
      createElement("h4", "conflict-summary", conflict.summary),
      createElement("p", "conflict-rationale", conflict.rationale),
      source,
    );
    if (conflict.resolution_note) {
      card.append(createElement("p", "conflict-resolution", conflict.resolution_note));
    }
    const history = createElement("button", "text-button", "History");
    history.type = "button";
    history.addEventListener("click", async () => {
      try {
        showResourceHistory(card, await getConflictRevisions(conflict.id));
      } catch (error) {
        showFeedback(elements.sectionFeedback, error.message, true);
      }
    });
    card.append(history);
    if (["clinician", "admin"].includes(DEMO_CONTEXT.actorRole)) {
      const status = createSelect(
        "Adjudication",
        {
          clinician_confirmed: "Clinician confirmed",
          resolved: "Resolved",
          needs_review: "Reopen for review",
        },
        conflict.status,
        `conflict-status-${conflict.id}`,
      );
      const note = createElement("textarea", "conflict-note");
      note.rows = 2;
      note.placeholder = "Resolution or verification note";
      note.value = conflict.resolution_note || "";
      const save = createElement("button", "primary-button", "Save adjudication");
      save.type = "button";
      save.addEventListener("click", async () => {
        try {
          await updateConflict(conflict.id, {
            expected_version: conflict.version,
            status: status.select.value,
            resolution_note: note.value.trim() || null,
          });
          await loadPatient();
        } catch (error) {
          showFeedback(elements.sectionFeedback, error.message, true);
        }
      });
      card.append(status.wrapper, note, save);
    }
    elements.conflictList.append(card);
  });
}

function renderHighlights(highlights) {
  elements.highlightList.replaceChildren();
  elements.highlightCount.textContent = `${highlights.length} priority item${highlights.length === 1 ? "" : "s"}`;
  highlights.forEach((highlight, index) => {
    if (!recordedImpressions.has(highlight.id)) {
      recordedImpressions.add(highlight.id);
      recordHighlightImpression(highlight.id, highlight.version).catch(() => {
        recordedImpressions.delete(highlight.id);
      });
    }
    const card = createElement(
      "article",
      `highlight-card priority-${index + 1} action-${highlight.action_status}`,
    );
    const priority = createElement(
      "div",
      "priority-marker",
      String(index + 1).padStart(2, "0"),
    );
    const body = createElement("div", "highlight-body");
    const top = createElement("div", "highlight-topline");
    top.append(
      createElement("span", `status-chip ${highlight.status}`, statusLabels[highlight.status]),
      createElement("span", "priority-score", `Priority ${highlight.priority}`),
    );
    body.append(
      top,
      createElement("h3", "highlight-text", highlight.text),
      createElement("p", "risk-reason", highlight.risk_reason),
    );
    const explanation = createElement("details", "priority-explanation");
    const explanationSummary = createElement(
      "summary",
      "",
      `Why this priority is ${highlight.priority}`,
    );
    const factorList = createElement("ul", "priority-factor-list");
    highlight.priority_factors.forEach((factor) => {
      const item = createElement("li", "priority-factor");
      item.append(
        createElement("strong", "", `${factor.points >= 0 ? "+" : ""}${factor.points} ${factor.label}`),
        createElement("span", "", factor.explanation),
      );
      factorList.append(item);
    });
    explanation.append(explanationSummary, factorList);
    body.append(explanation);
    const footer = createElement("div", "highlight-footer");
    const action = createElement("p", "suggested-action");
    action.append(
      createElement("span", "action-label", "Suggested action"),
      document.createTextNode(highlight.suggested_action),
    );
    const sourceButton = createElement("button", "text-button", "View exact source");
    sourceButton.type = "button";
    const openSource = () => {
      const target = document.querySelector(`#source-${highlight.id}`)
        || document.querySelector(`#entry-${highlight.provenance_pointer.entry_id}`);
      if (!target) return;
      document.querySelectorAll(".source-target").forEach((node) => {
        node.classList.remove("source-target");
      });
      target.classList.add("source-target");
      target.scrollIntoView({ behavior: "smooth", block: "center" });
    };
    sourceButton.addEventListener("click", openSource);
    const claimButton = createElement("button", "text-button", "View original claim");
    claimButton.type = "button";
    claimButton.disabled = !highlight.source_evidence_pointer;
    claimButton.addEventListener("click", async () => {
      try {
        const evidence = await getHighlightSource(highlight.id);
        let evidenceBox = body.querySelector(".source-evidence");
        if (!evidenceBox) {
          evidenceBox = createElement("div", "source-evidence");
          body.append(evidenceBox);
        }
        evidenceBox.replaceChildren(
          createElement("strong", "", evidence.artifact.label),
          createElement("blockquote", "", evidence.excerpt),
        );
      } catch (error) {
        showFeedback(feedback, error.message, true);
      }
    });
    const historyButton = createElement("button", "text-button", "Version history");
    historyButton.type = "button";
    historyButton.addEventListener("click", async () => {
      try {
        showResourceHistory(body, await getHighlightRevisions(highlight.id));
      } catch (error) {
        showFeedback(feedback, error.message, true);
      }
    });
    footer.append(action, sourceButton, claimButton, historyButton);

    const controls = createElement("div", "highlight-controls");
    const reviewSelect = createSelect(
      "Review status",
      statusLabels,
      highlight.status,
      `review-${highlight.id}`,
    );
    if (DEMO_CONTEXT.actorRole === "staff") {
      [...reviewSelect.select.options].forEach((option) => {
        option.disabled = ["clinician_confirmed", "rejected"].includes(option.value);
      });
    }
    const assignmentSelect = createSelect(
      "Assigned to",
      { "": "Unassigned", staff: "Staff", clinician: "Clinician", admin: "Admin" },
      highlight.assigned_to || "",
      `assignee-${highlight.id}`,
    );
    const actionSelect = createSelect(
      "Action status",
      actionStatusLabels,
      highlight.action_status,
      `action-${highlight.id}`,
    );
    controls.append(reviewSelect.wrapper, assignmentSelect.wrapper, actionSelect.wrapper);

    const noteLabel = createElement("label", "field-label", "Disposition note");
    noteLabel.htmlFor = `note-${highlight.id}`;
    const note = createElement("textarea", "highlight-note");
    note.id = `note-${highlight.id}`;
    note.rows = 2;
    note.maxLength = 1000;
    note.value = highlight.disposition_note || "";
    const save = createElement("button", "primary-button", "Save update");
    save.type = "button";
    const feedback = createElement("span", "form-feedback");
    save.addEventListener("click", async () => {
      save.disabled = true;
      showFeedback(feedback, "Saving…");
      try {
        await updateHighlight(highlight.id, {
          expected_version: highlight.version,
          status: reviewSelect.select.value,
          assigned_to: assignmentSelect.select.value || null,
          action_status: actionSelect.select.value,
          disposition_note: note.value.trim() || null,
        });
        await loadPatient();
      } catch (error) {
        showFeedback(feedback, error.message, true);
        save.disabled = false;
      }
    });
    const updateActions = createElement("div", "highlight-update-actions");
    updateActions.append(save, feedback);
    body.append(footer, controls, noteLabel, note, updateActions);
    card.append(priority, body);
    elements.highlightList.append(card);
  });
}

function createSelect(label, options, selectedValue, id) {
  const wrapper = createElement("label", "compact-field");
  const labelText = createElement("span", "field-label", label);
  const select = createElement("select");
  select.id = id;
  Object.entries(options).forEach(([value, text]) => {
    const option = createElement("option", "", text);
    option.value = value;
    option.selected = value === selectedValue;
    select.append(option);
  });
  wrapper.append(labelText, select);
  return { wrapper, select };
}

function appendEntryContent(container, entry, highlights) {
  const pointers = highlights
    .filter((highlight) => highlight.provenance_pointer.entry_id === entry.id)
    .sort((left, right) => left.provenance_pointer.start - right.provenance_pointer.start);
  let cursor = 0;
  pointers.forEach((highlight) => {
    const { start, end } = highlight.provenance_pointer;
    if (start < cursor || end > entry.content.length) return;
    container.append(document.createTextNode(entry.content.slice(cursor, start)));
    const mark = createElement("mark", "provenance-mark", entry.content.slice(start, end));
    mark.id = `source-${highlight.id}`;
    container.append(mark);
    cursor = end;
  });
  container.append(document.createTextNode(entry.content.slice(cursor)));
}

function useSelectedTimelineText(entry, content) {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
    showFeedback(
      elements.highlightCreateFeedback,
      "Select a phrase in this entry first.",
      true,
    );
    return;
  }
  const range = selection.getRangeAt(0);
  if (!content.contains(range.startContainer) || !content.contains(range.endContainer)) {
    showFeedback(
      elements.highlightCreateFeedback,
      "The selection must stay within one timeline entry.",
      true,
    );
    return;
  }
  const before = document.createRange();
  before.selectNodeContents(content);
  before.setEnd(range.startContainer, range.startOffset);
  const start = before.toString().length;
  const text = range.toString();
  selectedHighlightSource = {
    entryId: entry.id,
    start,
    end: start + text.length,
    text,
  };
  elements.selectedSource.textContent = `“${text}” — ${entry.title}`;
  elements.createHighlight.disabled = false;
  showFeedback(elements.highlightCreateFeedback, "Source captured. Complete the fields below.");
  elements.highlightComposerCard.scrollIntoView({ behavior: "smooth", block: "center" });
}

function useSelectedCommentText(entry, content) {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
    showFeedback(elements.commentFeedback, "Select a phrase in this entry first.", true);
    return;
  }
  const range = selection.getRangeAt(0);
  if (!content.contains(range.startContainer) || !content.contains(range.endContainer)) {
    showFeedback(elements.commentFeedback, "The selection must stay within one entry.", true);
    return;
  }
  const before = document.createRange();
  before.selectNodeContents(content);
  before.setEnd(range.startContainer, range.startOffset);
  const start = before.toString().length;
  selectedCommentSpanTarget = {
    resource_type: "entry",
    resource_id: entry.id,
    start,
    end: start + range.toString().length,
  };
  elements.commentContent.focus();
  showFeedback(elements.commentFeedback, `Comment will attach to “${range.toString()}”.`);
}

function renderTimeline(entries, highlights) {
  elements.timelineList.replaceChildren();
  elements.entryCount.textContent = `${entries.length} timeline entr${entries.length === 1 ? "y" : "ies"}`;
  entries.forEach((entry) => {
    const item = createElement(
      "article",
      `timeline-entry role-${entry.author_role} type-${entry.type}`,
    );
    item.id = `entry-${entry.id}`;
    const rail = createElement("div", "timeline-rail");
    rail.append(createElement("span", "timeline-dot"));
    const card = createElement("div", "entry-card");
    const meta = createElement("div", "entry-meta");
    meta.append(
      createElement("span", "entry-type", entryTypeLabels[entry.type] || entry.type),
      createElement("span", "entry-role", roleLabels[entry.author_role] || entry.author_role),
      createElement("time", "entry-time", formatDateTime(entry.timestamp)),
    );
    card.append(meta, createElement("h3", "entry-title", entry.title));
    if (entry.type.startsWith("ai_")) {
      card.append(
        createElement(
          "p",
          "ai-disclaimer",
          "AI-generated content · Review against the original session; not clinically confirmed",
        ),
      );
    }
    const content = createElement("p", "entry-content");
    appendEntryContent(content, entry, highlights);
    card.append(content);
    const footer = createElement("div", "entry-footer");
    footer.append(
      createElement("span", "origin-label", `Source: ${entry.origin.source_label}`),
    );
    if (entry.origin.source_pointer && DEMO_CONTEXT.actorRole !== "patient") {
      const evidenceButton = createElement("button", "text-button", "View original evidence");
      evidenceButton.type = "button";
      evidenceButton.addEventListener("click", async () => {
        evidenceButton.disabled = true;
        try {
          const evidence = await getEntrySource(entry.id);
          let evidenceBox = card.querySelector(".source-evidence");
          if (!evidenceBox) {
            evidenceBox = createElement("div", "source-evidence");
            card.append(evidenceBox);
          }
          evidenceBox.replaceChildren(
            createElement("strong", "", evidence.artifact.label),
            createElement("blockquote", "", evidence.excerpt),
            createElement("pre", "", evidence.artifact.content),
          );
          evidenceBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
        } catch (error) {
          showFeedback(elements.commentFeedback, error.message, true);
        } finally {
          evidenceButton.disabled = false;
        }
      });
      footer.append(evidenceButton);
    }
    if (entry.review_status) {
      footer.append(
        createElement(
          "span",
          `status-chip ${entry.review_status}`,
          statusLabels[entry.review_status],
        ),
      );
    }
    if (DEMO_CONTEXT.actorRole === "clinician") {
      const selectButton = createElement(
        "button",
        "text-button entry-selection-action",
        "Use selected text",
      );
      selectButton.type = "button";
      selectButton.addEventListener("mousedown", (event) => event.preventDefault());
      selectButton.addEventListener("click", () => useSelectedTimelineText(entry, content));
      footer.append(selectButton);
    }
    if (DEMO_CONTEXT.actorRole !== "patient") {
      const commentButton = createElement(
        "button",
        "text-button entry-selection-action",
        "Comment on selected text",
      );
      commentButton.type = "button";
      commentButton.addEventListener("mousedown", (event) => event.preventDefault());
      commentButton.addEventListener("click", () => useSelectedCommentText(entry, content));
      footer.append(commentButton);
    }
    card.append(footer);
    item.append(rail, card);
    elements.timelineList.append(item);
  });
}

function renderCommentTargets(entries, sections) {
  elements.commentTarget.replaceChildren();
  sections.forEach((section) => {
    const option = createElement("option", "", `Section: ${section.section}`);
    option.value = `section:${section.section}`;
    elements.commentTarget.append(option);
  });
  entries.forEach((entry) => {
    const option = createElement("option", "", `Entry: ${entry.title}`);
    option.value = `entry:${entry.id}`;
    elements.commentTarget.append(option);
  });
}

function selectedCommentTarget() {
  if (selectedCommentSpanTarget) return selectedCommentSpanTarget;
  const [resourceType, ...resourceIdParts] = elements.commentTarget.value.split(":");
  return {
    resource_type: resourceType,
    resource_id: resourceIdParts.join(":"),
  };
}

function renderAudit(events) {
  elements.auditList.replaceChildren();
  elements.auditCount.textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
  if (events.length === 0) {
    elements.auditList.append(
      createElement("p", "helper-text", "No audited changes in this session yet."),
    );
    return;
  }
  [...events].reverse().forEach((event) => {
    const item = createElement("article", "audit-item");
    item.append(
      createElement(
        "span",
        "audit-operation",
        `${event.operation.replaceAll("_", " ")} · ${event.resource} v${event.version}`,
      ),
      createElement("span", "audit-actor", `Actor ${event.changed_by}`),
      createElement("time", "", formatDateTime(event.changed_at)),
    );
    elements.auditList.append(item);
  });
}

function renderSection(sections) {
  const plan = sections.find((section) => section.section === "plan");
  currentPlanVersion = plan?.version || 0;
  elements.sectionContent.value = plan?.content || "";
  elements.sectionVersion.textContent = `Version ${currentPlanVersion}`;
}

function renderComments(comments) {
  elements.commentList.replaceChildren();
  elements.commentCount.textContent = `${comments.length} comment${comments.length === 1 ? "" : "s"}`;
  comments.forEach((comment) => {
    const item = createElement(
      "article",
      `comment-item${comment.resolved ? " resolved" : ""}${comment.parent_id ? " reply" : ""}`,
    );
    const meta = createElement("div", "comment-meta");
    meta.append(
      createElement(
        "span",
        "",
        `${roleLabels[comment.author_role] || comment.author_role} · ${formatDateTime(comment.created_at)}`,
      ),
    );
    const toggle = createElement(
      "button",
      "text-button",
      comment.resolved ? "Reopen" : "Resolve",
    );
    toggle.type = "button";
    toggle.addEventListener("click", async () => {
      try {
        await setCommentResolved(comment.id, !comment.resolved, comment.version);
        await loadPatient();
      } catch (error) {
        showFeedback(elements.commentFeedback, error.message, true);
      }
    });
    const reply = createElement("button", "text-button", "Reply");
    reply.type = "button";
    reply.addEventListener("click", () => {
      replyToCommentId = comment.id;
      elements.commentContent.focus();
      showFeedback(elements.commentFeedback, "Your next comment will be added as a reply.");
    });
    const history = createElement("button", "text-button", "History");
    history.type = "button";
    history.addEventListener("click", async () => {
      try {
        showResourceHistory(item, await getCommentRevisions(comment.id));
      } catch (error) {
        showFeedback(elements.commentFeedback, error.message, true);
      }
    });
    const controls = createElement("span", "comment-controls");
    controls.append(reply, toggle, history);
    meta.append(controls);
    item.append(meta, createElement("p", "comment-content", comment.content));
    const targetLabel = comment.target.resource_type === "entry"
      ? `Attached to timeline entry ${comment.target.resource_id}`
      : `Attached to section ${comment.target.resource_id}`;
    item.append(createElement("span", "comment-target", targetLabel));
    if (comment.target.start !== null && comment.target.start !== undefined) {
      item.append(
        createElement(
          "span",
          "comment-target",
          ` · exact span ${comment.target.start}–${comment.target.end}`,
        ),
      );
    }
    comment.mentions.forEach((mention) => {
      item.append(createElement("span", "mention-chip", `@${mention}`));
    });
    if (comment.assigned_to) {
      item.append(
        createElement("span", "assignment-chip", `Assigned: ${comment.assigned_to}`),
      );
    }
    elements.commentList.append(item);
  });
}

function showFeedback(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("error", isError);
}

function connectRealtime() {
  realtimeSocket?.close();
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  realtimeSocket = new WebSocket(
    `${protocol}//${location.host}/api/ws/patients/${DEMO_CONTEXT.patientId}`,
  );
  let refreshTimer = null;
  realtimeSocket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "presence") {
      elements.presenceStatus.hidden = false;
      elements.presenceStatus.textContent = `${message.count} online`;
    }
    if (message.type === "refresh" && !refreshTimer) {
      refreshTimer = window.setTimeout(async () => {
        refreshTimer = null;
        await loadPatient();
      }, 250);
    }
  });
  realtimeSocket.addEventListener("close", () => {
    elements.presenceStatus.hidden = true;
  });
}

function showResourceHistory(container, revisions) {
  let history = container.querySelector(".resource-history");
  if (!history) {
    history = createElement("div", "resource-history");
    container.append(history);
  }
  history.replaceChildren();
  if (revisions.length > 1) {
    const compare = createElement("div", "version-compare-controls");
    const from = createElement("select");
    const to = createElement("select");
    revisions.forEach((revision) => {
      const first = createElement("option", "", `Version ${revision.version}`);
      first.value = String(revision.version);
      from.append(first);
      to.append(first.cloneNode(true));
    });
    from.value = String(revisions[0].version);
    to.value = String(revisions.at(-1).version);
    const run = createElement("button", "secondary-button", "Compare A/B");
    const output = createElement("pre", "resource-snapshot");
    output.hidden = true;
    run.type = "button";
    run.addEventListener("click", () => {
      const before = revisions.find((item) => item.version === Number(from.value))?.snapshot || {};
      const after = revisions.find((item) => item.version === Number(to.value))?.snapshot || {};
      const changes = {};
      new Set([...Object.keys(before), ...Object.keys(after)]).forEach((key) => {
        if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) {
          changes[key] = { A: before[key], B: after[key] };
        }
      });
      output.hidden = false;
      output.textContent = JSON.stringify(changes, null, 2) || "No differences.";
    });
    compare.append(from, to, run);
    history.append(compare, output);
  }
  revisions.forEach((revision, index) => {
    const previous = revisions[index - 1]?.snapshot || {};
    const changedFields = Object.keys(revision.snapshot).filter(
      (key) => JSON.stringify(previous[key]) !== JSON.stringify(revision.snapshot[key]),
    );
    const item = createElement("details", "resource-history-item");
    item.append(
      createElement(
        "summary",
        "",
        `v${revision.version} · ${revision.operation} · ${formatDateTime(revision.changed_at)}`,
      ),
      createElement(
        "p",
        "resource-change-list",
        `Changed fields: ${changedFields.join(", ") || "none"}`,
      ),
      createElement("pre", "resource-snapshot", JSON.stringify(revision.snapshot, null, 2)),
    );
    history.append(item);
  });
}

async function renderHistory() {
  try {
    const revisions = await getSectionRevisions("plan");
    elements.revisionList.replaceChildren();
    elements.compareFrom.replaceChildren();
    elements.compareTo.replaceChildren();
    revisions.forEach((revision) => {
      const fromOption = createElement("option", "", `Version ${revision.version}`);
      fromOption.value = revision.version;
      const toOption = fromOption.cloneNode(true);
      elements.compareFrom.append(fromOption);
      elements.compareTo.append(toOption);
    });
    if (revisions.length) {
      elements.compareFrom.value = String(revisions[0].version);
      elements.compareTo.value = String(revisions.at(-1).version);
    }
    [...revisions].reverse().forEach((revision, index) => {
      const item = createElement("article", "revision-item");
      const meta = createElement("div", "revision-meta");
      meta.append(
        createElement(
          "span",
          "",
          `Version ${revision.version} · ${revision.operation} · ${formatDateTime(revision.changed_at)}`,
        ),
      );
      if (index > 0) {
        const button = createElement("button", "text-button", "Revert to this");
        button.type = "button";
        button.addEventListener("click", async () => {
          try {
            await revertSection("plan", revision.version);
            showFeedback(elements.sectionFeedback, `Reverted to version ${revision.version}.`);
            await loadPatient();
            await renderHistory();
          } catch (error) {
            showFeedback(elements.sectionFeedback, error.message, true);
          }
        });
        meta.append(button);
      }
      item.append(meta, createElement("p", "revision-content", revision.content));
      if (revision.diff) {
        item.append(createElement("pre", "revision-diff", revision.diff));
      }
      elements.revisionList.append(item);
    });
  } catch (error) {
    showFeedback(elements.sectionFeedback, error.message, true);
  }
}

async function loadPatient() {
  setScreenState("loading");
  try {
    const detail = await getPatientDetail();
    renderPatient(detail.patient);
    renderConflicts(detail.conflicts || []);
    renderHighlights(detail.highlights);
    renderTimeline(detail.entries, detail.highlights);
    renderSection(detail.sections || []);
    renderComments(detail.comments || []);
    renderCommentTargets(detail.entries, detail.sections || []);
    renderPatientActions(detail.patient_actions || []);
    const isPatient = DEMO_CONTEXT.actorRole === "patient";
    if (!isPatient) {
      try {
        renderAudit(await getAuditEvents());
      } catch (error) {
        renderAudit([]);
        console.error("Audit request failed", error);
      }
    }
    elements.glancePanel.hidden = isPatient;
    elements.patientDashboard.hidden = !isPatient;
    elements.workspacePanel.hidden = isPatient;
    elements.staffNoteCard.hidden = DEMO_CONTEXT.actorRole !== "staff";
    elements.highlightComposerCard.hidden = DEMO_CONTEXT.actorRole !== "clinician";
    elements.aiIngestCard.hidden = !["staff", "clinician", "admin"].includes(
      DEMO_CONTEXT.actorRole,
    );
    elements.sectionContent.disabled = DEMO_CONTEXT.actorRole !== "clinician";
    elements.saveSection.disabled = DEMO_CONTEXT.actorRole !== "clinician";
    const isEmpty = detail.highlights.length === 0 && detail.entries.length === 0;
    setScreenState(isEmpty ? "empty" : "ready");
    return detail;
  } catch (error) {
    clearPatient();
    if (error instanceof ApiError && error.status === 401) {
      showLogin();
    } else if (error instanceof ApiError && error.status === 403) {
      setScreenState("forbidden");
    } else if (error instanceof ApiError && error.status === 404) {
      setScreenState("notFound");
    } else {
      setScreenState("error");
    }
    console.error("Patient detail request failed", error);
    return null;
  }
}

elements.retryButton.addEventListener("click", loadPatient);
elements.loginButton.addEventListener("click", async () => {
  elements.loginButton.disabled = true;
  showFeedback(elements.loginFeedback, "Signing in…");
  try {
    const session = await login(
      elements.loginUsername.value.trim(),
      elements.loginPassword.value,
    );
    showAuthenticatedSession(session);
    await loadPatient();
  } catch (error) {
    showFeedback(elements.loginFeedback, error.message, true);
  } finally {
    elements.loginButton.disabled = false;
  }
});
elements.loginPassword.addEventListener("keydown", (event) => {
  if (event.key === "Enter") elements.loginButton.click();
});
elements.logoutButton.addEventListener("click", async () => {
  try {
    await logout();
  } finally {
    showLogin();
  }
});
elements.addEntry.addEventListener("click", async () => {
  const title = elements.entryTitle.value.trim();
  const content = elements.entryContent.value.trim();
  if (!title || !content) {
    showFeedback(elements.entryFeedback, "Enter both a title and note content.", true);
    return;
  }
  elements.addEntry.disabled = true;
  showFeedback(elements.entryFeedback, "Adding note…");
  try {
    await addTimelineEntry(title, content);
    elements.entryTitle.value = "";
    elements.entryContent.value = "";
    await loadPatient();
    showFeedback(elements.entryFeedback, "Staff note added to the timeline.");
  } catch (error) {
    showFeedback(elements.entryFeedback, error.message, true);
  } finally {
    elements.addEntry.disabled = false;
  }
});
elements.addPatientEntry.addEventListener("click", async () => {
  const title = elements.patientEntryTitle.value.trim();
  const content = elements.patientEntryContent.value.trim();
  if (!title || !content) {
    showFeedback(
      elements.patientEntryFeedback,
      "Enter both a title and an update for your care team.",
      true,
    );
    return;
  }
  elements.addPatientEntry.disabled = true;
  showFeedback(elements.patientEntryFeedback, "Sharing update…");
  try {
    await addTimelineEntry(title, content);
    elements.patientEntryTitle.value = "";
    elements.patientEntryContent.value = "";
    await loadPatient();
    showFeedback(elements.patientEntryFeedback, "Your update was added to the timeline.");
  } catch (error) {
    showFeedback(elements.patientEntryFeedback, error.message, true);
  } finally {
    elements.addPatientEntry.disabled = false;
  }
});
elements.createHighlight.addEventListener("click", async () => {
  const reason = elements.highlightReason.value.trim();
  const action = elements.highlightAction.value.trim();
  if (!selectedHighlightSource || !reason || !action) {
    showFeedback(
      elements.highlightCreateFeedback,
      "Select source text and enter a risk reason and suggested action.",
      true,
    );
    return;
  }
  elements.createHighlight.disabled = true;
  showFeedback(elements.highlightCreateFeedback, "Creating highlight…");
  try {
    await createHighlight({
      entry_id: selectedHighlightSource.entryId,
      start: selectedHighlightSource.start,
      end: selectedHighlightSource.end,
      risk_reason: reason,
      suggested_action: action,
      patient_instruction: elements.highlightPatientInstruction.value.trim() || null,
      risk_level: elements.highlightRiskLevel.value,
      clinical_entities: elements.highlightEntities.value
        .split(",")
        .map((value) => value.trim().toLowerCase())
        .filter(Boolean),
      status: elements.highlightStatus.value,
      assigned_to: elements.highlightAssignee.value || null,
    });
    selectedHighlightSource = null;
    elements.selectedSource.textContent = "No source text selected.";
    elements.highlightReason.value = "";
    elements.highlightAction.value = "";
    elements.highlightPatientInstruction.value = "";
    elements.highlightEntities.value = "";
    await loadPatient();
    showFeedback(elements.highlightCreateFeedback, "Highlight created and audited.");
  } catch (error) {
    showFeedback(elements.highlightCreateFeedback, error.message, true);
    elements.createHighlight.disabled = false;
  }
});
elements.saveSection.addEventListener("click", async () => {
  showFeedback(elements.sectionFeedback, "Saving…");
  try {
    const section = await updateSection(
      "plan",
      elements.sectionContent.value,
      currentPlanVersion,
    );
    currentPlanVersion = section.version;
    elements.sectionVersion.textContent = `Version ${section.version}`;
    showFeedback(elements.sectionFeedback, `Saved version ${section.version}.`);
    await renderHistory();
  } catch (error) {
    showFeedback(elements.sectionFeedback, error.message, true);
  }
});
elements.showHistory.addEventListener("click", renderHistory);
elements.compareVersions.addEventListener("click", async () => {
  try {
    const comparison = await compareSectionRevisions(
      "plan",
      Number(elements.compareFrom.value),
      Number(elements.compareTo.value),
    );
    elements.versionCompareOutput.hidden = false;
    elements.versionCompareOutput.textContent = comparison.diff || "No differences.";
  } catch (error) {
    showFeedback(elements.sectionFeedback, error.message, true);
  }
});
elements.commentOnPlanSelection.addEventListener("click", () => {
  const start = elements.sectionContent.selectionStart;
  const end = elements.sectionContent.selectionEnd;
  if (start === end) {
    showFeedback(elements.commentFeedback, "Select text in the Care Plan first.", true);
    return;
  }
  selectedCommentSpanTarget = {
    resource_type: "section",
    resource_id: "plan",
    start,
    end,
  };
  showFeedback(
    elements.commentFeedback,
    `Comment will attach to Care Plan characters ${start}–${end}.`,
  );
  elements.commentContent.focus();
});
elements.addComment.addEventListener("click", async () => {
  const content = elements.commentContent.value.trim();
  if (!content) {
    showFeedback(elements.commentFeedback, "Enter a comment first.", true);
    return;
  }
  try {
    const target = replyToCommentId ? null : selectedCommentTarget();
    await addComment(content, elements.commentAssignee.value, replyToCommentId, target);
    replyToCommentId = null;
    selectedCommentSpanTarget = null;
    elements.commentContent.value = "";
    elements.commentAssignee.value = "";
    showFeedback(elements.commentFeedback, "Comment added.");
    await loadPatient();
  } catch (error) {
    showFeedback(elements.commentFeedback, error.message, true);
  }
});
elements.aiIngest.addEventListener("click", async () => {
  const title = elements.aiTitle.value.trim();
  const rawText = elements.aiRawText.value.trim();
  if (!title || !rawText) {
    showFeedback(elements.aiFeedback, "Enter a title and source text first.", true);
    return;
  }
  elements.aiIngest.disabled = true;
  showFeedback(elements.aiFeedback, "Redacting and summarizing…");
  try {
    const result = await ingestAI(title, rawText, elements.aiSource.value);
    const redacted = Object.values(result.redaction_counts).reduce((sum, value) => sum + value, 0);
    showFeedback(
      elements.aiFeedback,
      `AI summary created for clinical review; ${redacted} identifier(s) redacted.`,
    );
    elements.aiRawText.value = "";
    await loadPatient();
  } catch (error) {
    showFeedback(elements.aiFeedback, error.message, true);
  } finally {
    elements.aiIngest.disabled = false;
  }
});
elements.recordAudio.addEventListener("click", async () => {
  if (mediaRecorder?.state === "recording") {
    mediaRecorder.stop();
    elements.recordAudio.textContent = "Start recording";
    return;
  }
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    showFeedback(elements.aiFeedback, "Audio recording is unavailable in this browser.", true);
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.addEventListener("dataavailable", (event) => audioChunks.push(event.data));
    mediaRecorder.addEventListener("stop", async () => {
      stream.getTracks().forEach((track) => track.stop());
      const blob = new Blob(audioChunks, { type: mediaRecorder.mimeType || "audio/webm" });
      showFeedback(elements.aiFeedback, "Transcribing recording…");
      try {
        const transcript = await transcribeAudio(blob);
        elements.aiRawText.value = transcript.text;
        const detail = transcript.diarization_available
          ? "Speaker labels are available."
          : "Speaker identity was not inferred; segments are marked unknown.";
        showFeedback(elements.aiFeedback, `Transcription ready. ${detail}`);
      } catch (error) {
        showFeedback(elements.aiFeedback, error.message, true);
      }
    });
    mediaRecorder.start();
    elements.recordAudio.textContent = "Stop and transcribe";
    showFeedback(elements.aiFeedback, "Recording locally…");
  } catch (error) {
    showFeedback(elements.aiFeedback, `Microphone access failed: ${error.message}`, true);
  }
});
async function bootstrap() {
  try {
    const session = await getSession();
    showAuthenticatedSession(session);
    await loadPatient();
  } catch {
    showLogin();
  }
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/service-worker.js").catch(() => {});
}

bootstrap();
