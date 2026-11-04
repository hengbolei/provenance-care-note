// The Clinical Review Agent panel: start a review, show its draft, collect decisions, commit.
import {
  ApiError,
  commitAgentRun,
  discardAgentRun,
  getAgentRun,
  reviseAgentRun,
  startAgentRun,
} from "/static/api.js";
import { createElement, labelFor, showFeedback } from "/static/dom.js";
import { getLocale, t } from "/static/i18n.js";

const elements = {
  agentRunStatus: document.querySelector("#agent-run-status"),
  agentStart: document.querySelector("#agent-start"),
  agentProgress: document.querySelector("#agent-progress"),
  agentFeedback: document.querySelector("#agent-feedback"),
  agentDraft: document.querySelector("#agent-draft"),
  agentSummary: document.querySelector("#agent-summary"),
  agentClaimList: document.querySelector("#agent-claim-list"),
  agentQuestions: document.querySelector("#agent-questions"),
  agentQuestionList: document.querySelector("#agent-question-list"),
  agentCheckPoints: document.querySelector("#agent-check-points"),
  agentCheckPointList: document.querySelector("#agent-check-point-list"),
  agentHighlights: document.querySelector("#agent-highlights"),
  agentHighlightList: document.querySelector("#agent-highlight-list"),
  agentReviewActions: document.querySelector("#agent-review-actions"),
  agentReviseInstruction: document.querySelector("#agent-revise-instruction"),
  agentCommit: document.querySelector("#agent-commit"),
  agentRevise: document.querySelector("#agent-revise"),
  agentDiscard: document.querySelector("#agent-discard"),
  agentBudget: document.querySelector("#agent-budget"),
  agentLatestReview: document.querySelector("#agent-latest-review"),
  agentLatestVersion: document.querySelector("#agent-latest-version"),
  agentLatestContent: document.querySelector("#agent-latest-content"),
  agentTimeline: document.querySelector("#agent-timeline"),
  agentTraceId: document.querySelector("#agent-trace-id"),
  agentTimelineList: document.querySelector("#agent-timeline-list"),
};

let agentRun = null;
let agentPollTimer = null;
let agentDecisions = new Map();
let agentHighlightAccepts = new Set();
// Check point index -> included; verified check points start included, others cannot be.
let agentCheckPointAccepts = new Map();
// Set by initAgentPanel: what the panel needs from the rest of the page.
const hooks = {
  getEntries: () => [],
  focusEntries: () => {},
  reloadPatient: async () => {},
};

const AGENT_RUN_STORAGE_KEY = "carenote.agentRunId";
const AGENT_STEPS = 4;
function rememberAgentRun(runId) {
  try {
    if (runId) sessionStorage.setItem(AGENT_RUN_STORAGE_KEY, runId);
    else sessionStorage.removeItem(AGENT_RUN_STORAGE_KEY);
  } catch {
    // Storage can be unavailable; the panel works without it.
  }
}

function storedAgentRunId() {
  try {
    return sessionStorage.getItem(AGENT_RUN_STORAGE_KEY);
  } catch {
    return null;
  }
}

function agentRunSummary(run) {
  const report = run.verification;
  if (!report) return { text: "", warning: false };
  const unsupported = report.checks.filter((check) => check.failures.length).length;
  const parts = [
    t("agent.summaryVerified", {
      verified: report.checks.length - unsupported,
      total: report.checks.length,
    }),
  ];
  if (unsupported) parts.push(t("agent.summaryUnsupported", { count: unsupported }));
  if (report.uncovered_conflict_ids.length) {
    parts.push(t("agent.summaryUncovered", { count: report.uncovered_conflict_ids.length }));
  }
  const failedPoints = (report.check_points || []).filter((item) => item.failures.length).length;
  if (failedPoints) parts.push(t("agent.summaryCheckPointsUnsupported", { count: failedPoints }));
  const flagged = report.instruction_like_entry_ids?.length || 0;
  const withheld = report.withheld?.length || 0;
  if (flagged) parts.push(t("agent.summaryInstructionLike", { count: flagged }));
  if (withheld) parts.push(t("agent.summaryWithheld", { count: withheld }));
  return {
    text: parts.join(" · "),
    warning:
      unsupported > 0 || report.uncovered_conflict_ids.length > 0 || flagged > 0 || failedPoints > 0,
  };
}

function focusCitation(citation) {
  const entry = hooks.getEntries().find((item) => item.id === citation.entry_id);
  const content = document.querySelector(`#entry-${citation.entry_id} .entry-content`);
  if (!entry || !content || citation.end > entry.content.length) return;
  const mark = createElement(
    "mark",
    "provenance-mark citation-mark source-target",
    entry.content.slice(citation.start, citation.end),
  );
  content.replaceChildren(
    document.createTextNode(entry.content.slice(0, citation.start)),
    mark,
    document.createTextNode(entry.content.slice(citation.end)),
  );
  hooks.focusEntries([citation.entry_id]);
  mark.scrollIntoView({ behavior: "smooth", block: "center" });
}

function citationButton(citation, label, clickable) {
  const button = createElement(
    "button",
    "text-button agent-citation",
    t("agent.citationLabel", { label, quote: citation.quote }),
  );
  button.type = "button";
  button.title = clickable ? t("agent.citationShow") : t("agent.citationUnverified");
  button.disabled = !clickable;
  if (clickable) button.addEventListener("click", () => focusCitation(citation));
  return button;
}

function decisionControls(index, decision, verified, editable) {
  const controls = createElement("div", "agent-decision");
  const decisionLabel = createElement("label", "field-label", t("agent.decision"));
  const select = document.createElement("select");
  select.id = `agent-claim-decision-${index}`;
  decisionLabel.htmlFor = select.id;
  ["accept", "edit", "reject", "skip"].forEach((value) => {
    const option = createElement("option", "", t(`agent.decision.${value}`));
    option.value = value;
    option.disabled = value === "accept" && !verified;
    select.append(option);
  });
  select.value = decision.action;
  select.disabled = !editable;
  const editBox = document.createElement("textarea");
  editBox.rows = 3;
  editBox.maxLength = 400;
  editBox.value = decision.text;
  editBox.disabled = !editable;
  editBox.setAttribute("aria-label", t("agent.editedText", { number: index + 1 }));
  const reasonBox = document.createElement("input");
  reasonBox.maxLength = 500;
  reasonBox.placeholder = t("agent.rejectReason");
  reasonBox.value = decision.reason;
  reasonBox.disabled = !editable;
  reasonBox.setAttribute("aria-label", t("agent.rejectReasonFor", { number: index + 1 }));
  const sync = () => {
    editBox.hidden = decision.action !== "edit";
    reasonBox.hidden = decision.action !== "reject";
  };
  select.addEventListener("change", () => {
    decision.action = select.value;
    sync();
  });
  editBox.addEventListener("input", () => {
    decision.text = editBox.value;
  });
  reasonBox.addEventListener("input", () => {
    decision.reason = reasonBox.value;
  });
  sync();
  controls.append(decisionLabel, select, editBox, reasonBox);
  return controls;
}

function renderAgentClaims(run, editable) {
  elements.agentClaimList.replaceChildren();
  const checks = run.verification?.checks || [];
  if (!run.draft.claims.length) {
    elements.agentClaimList.append(createElement("p", "helper-text", t("agent.noClaims")));
  }
  run.draft.claims.forEach((claim, index) => {
    const check = checks[index] || { failures: [], citations: [] };
    const verified = check.failures.length === 0;
    const card = createElement("div", `agent-claim ${verified ? "verified" : "unsupported"}`);
    const body = createElement("div");
    const meta = createElement("div", "agent-claim-meta");
    meta.append(
      createElement(
        "span",
        `agent-chip ${verified ? "verified" : "unsupported"}`,
        verified ? t("agent.verified") : t("agent.unsupported"),
      ),
      createElement("span", "agent-chip", t(`agent.category.${claim.category}`)),
      createElement(
        "span",
        `agent-chip risk-${claim.risk_level}`,
        t("agent.riskChip", { risk: labelFor("risk", claim.risk_level) }),
      ),
    );
    body.append(createElement("p", "agent-claim-text", claim.text), meta);
    if (!verified) {
      const failures = createElement("ul", "agent-failures");
      check.failures.forEach((failure) => {
        failures.append(createElement("li", "", t(`agent.failure.${failure}`)));
      });
      body.append(failures);
    }
    // Verified citations point into the original entry; unverified ones are shown as quoted.
    const citations = createElement("div", "agent-citations");
    const shown = check.citations.length ? check.citations : claim.citations;
    shown.forEach((citation, position) => {
      citations.append(
        citationButton(
          citation,
          t("agent.sourceNumbered", { number: position + 1 }),
          check.citations.length > 0,
        ),
      );
    });
    body.append(citations);
    if (!agentDecisions.has(index)) {
      agentDecisions.set(index, { action: verified ? "accept" : "skip", text: claim.text, reason: "" });
    }
    card.append(body, decisionControls(index, agentDecisions.get(index), verified, editable));
    elements.agentClaimList.append(card);
  });
}

function renderAgentCheckPoints(run, editable) {
  elements.agentCheckPointList.replaceChildren();
  const points = run.draft.check_points || [];
  const checks = run.verification?.check_points || [];
  elements.agentCheckPoints.hidden = points.length === 0;
  const labels = new Map(
    (run.references || []).map((item) => [`${item.document_id}:${item.version}`, item]),
  );
  points.forEach((point, index) => {
    const check = checks[index] || { failures: [], citations: [], reference: null };
    const verified = check.failures.length === 0;
    const card = createElement("div", `agent-claim ${verified ? "verified" : "unsupported"}`);
    const body = createElement("div");
    const meta = createElement("div", "agent-claim-meta");
    meta.append(
      createElement(
        "span",
        `agent-chip ${verified ? "verified" : "unsupported"}`,
        verified ? t("agent.verified") : t("agent.unsupported"),
      ),
    );
    const reference = check.reference || point.reference;
    const label = labels.get(`${reference.document_id}:${reference.version}`);
    if (label) meta.append(createElement("span", "agent-chip", t(`knowledge.kind.${label.kind}`)));
    body.append(createElement("p", "agent-claim-text", point.text), meta);
    if (!verified) {
      const failures = createElement("ul", "agent-failures");
      check.failures.forEach((failure) => {
        failures.append(createElement("li", "", t(`agent.checkPointFailure.${failure}`)));
      });
      body.append(failures);
    }
    const quote = createElement("blockquote", "agent-reference");
    quote.append(
      createElement(
        "span",
        "agent-reference-title",
        label
          ? t("agent.referenceLabel", { title: label.title, version: label.version, label: label.label })
          : t("agent.referenceUnknown"),
      ),
      document.createTextNode(reference.quote),
    );
    body.append(quote);
    const shown = check.citations.length ? check.citations : point.citations;
    if (shown.length) {
      const citations = createElement("div", "agent-citations");
      shown.forEach((citation, position) => {
        citations.append(
          citationButton(
            citation,
            t("agent.sourceNumbered", { number: position + 1 }),
            check.citations.length > 0,
          ),
        );
      });
      body.append(citations);
    }
    if (!agentCheckPointAccepts.has(index)) agentCheckPointAccepts.set(index, verified);
    const controls = createElement("div", "agent-decision");
    const include = createElement("label", "agent-checkbox");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = verified && agentCheckPointAccepts.get(index);
    checkbox.disabled = !editable || !verified;
    checkbox.addEventListener("change", () => agentCheckPointAccepts.set(index, checkbox.checked));
    include.append(
      checkbox,
      document.createTextNode(
        verified ? t("agent.checkPointInclude") : t("agent.checkPointNotVerified"),
      ),
    );
    controls.append(include);
    card.append(body, controls);
    elements.agentCheckPointList.append(card);
  });
}

function renderAgentHighlights(run, editable) {
  elements.agentHighlightList.replaceChildren();
  const candidates = run.draft.candidate_highlights;
  elements.agentHighlights.hidden = candidates.length === 0;
  candidates.forEach((candidate, index) => {
    const resolved = run.highlight_citations[index];
    const card = createElement("div", `agent-claim ${resolved ? "verified" : "unsupported"}`);
    const body = createElement("div");
    const citations = createElement("div", "agent-citations");
    citations.append(citationButton(resolved || candidate.citation, t("agent.source"), Boolean(resolved)));
    body.append(
      createElement("p", "agent-claim-text", candidate.risk_reason),
      createElement("p", "helper-text", t("agent.suggestedAction", { action: candidate.suggested_action })),
      citations,
    );
    const controls = createElement("div", "agent-decision");
    const label = createElement("label", "agent-checkbox");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = agentHighlightAccepts.has(index);
    checkbox.disabled = !editable || !resolved;
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) agentHighlightAccepts.add(index);
      else agentHighlightAccepts.delete(index);
    });
    label.append(
      checkbox,
      document.createTextNode(resolved ? t("agent.createHighlight") : t("agent.sourceNotVerified")),
    );
    controls.append(label);
    card.append(body, controls);
    elements.agentHighlightList.append(card);
  });
}

function agentFeedbackFor(run) {
  if (run.status === "failed") return [run.error || t("agent.failed"), true];
  if (run.stop_reason) return [run.stop_reason, run.status === "budget_exceeded"];
  if (run.status === "running") return [t("agent.preparing"), false];
  if (run.status === "committed") {
    return [t("agent.committedMessage"), false];
  }
  if (run.status === "discarded") return [t("agent.discardedMessage"), false];
  return ["", false];
}

function renderAgentTimeline(run) {
  const steps = run?.timeline || [];
  elements.agentTimeline.hidden = steps.length === 0;
  elements.agentTraceId.textContent = run?.trace_id ? t("agent.traceId", { id: run.trace_id }) : "";
  elements.agentTimelineList.replaceChildren(
    ...steps.map((step) => {
      const item = createElement("li", "agent-timeline-step");
      const details = [t("agent.timelineDuration", { duration: step.duration_ms.toLocaleString(getLocale()) })];
      const tokens = step.input_tokens + step.output_tokens;
      if (tokens) details.push(t("agent.timelineTokens", { count: tokens }));
      if (step.tool_calls) details.push(t("agent.timelineTools", { count: step.tool_calls }));
      if (step.unsupported_claims) {
        details.push(t("agent.timelineUnsupported", { count: step.unsupported_claims }));
      }
      if (step.model) details.push(step.prompt ? `${step.model} · ${step.prompt}` : step.model);
      item.append(
        createElement("span", "agent-timeline-node", t(`agent.node.${step.node}`)),
        document.createTextNode(` · ${details.join(" · ")}`),
      );
      return item;
    }),
  );
}

function renderAgentRun(run) {
  agentRun = run;
  renderAgentTimeline(run);
  elements.agentRunStatus.textContent = run ? t(`agent.status.${run.status}`) : t("agent.noReview");
  const running = run?.status === "running";
  elements.agentStart.disabled = running || run?.status === "awaiting_review";
  elements.agentProgress.hidden = !run;
  const completed = running ? Math.min(run.budget?.steps || 0, AGENT_STEPS) : AGENT_STEPS;
  elements.agentProgress.querySelectorAll("li").forEach((item) => {
    const step = Number(item.dataset.step);
    item.classList.toggle("done", Boolean(run) && step <= completed && run.status !== "failed");
    item.classList.toggle("active", running && step === completed + 1);
  });
  if (!run) {
    elements.agentDraft.hidden = true;
    showFeedback(elements.agentFeedback, "");
    return;
  }
  showFeedback(elements.agentFeedback, ...agentFeedbackFor(run));
  const hasDraft = Boolean(run.draft) && !running;
  elements.agentDraft.hidden = !hasDraft;
  if (!hasDraft) return;
  const editable = run.status === "awaiting_review";
  const summary = agentRunSummary(run);
  elements.agentSummary.textContent = summary.text;
  elements.agentSummary.classList.toggle("warning", summary.warning);
  renderAgentClaims(run, editable);
  renderAgentCheckPoints(run, editable);
  renderAgentHighlights(run, editable);
  elements.agentQuestions.hidden = run.draft.open_questions.length === 0;
  elements.agentQuestionList.replaceChildren(
    ...run.draft.open_questions.map((question) => createElement("li", "", question)),
  );
  elements.agentReviewActions.hidden = !editable;
  const budget = run.budget;
  elements.agentBudget.textContent = budget
    ? t("agent.budget", {
        steps: budget.steps,
        maxSteps: budget.max_steps,
        revisions: budget.revisions,
        maxRevisions: budget.max_revisions,
        autoRevisions: budget.auto_revisions,
        maxAutoRevisions: budget.max_auto_revisions,
        tokens: (budget.input_tokens + budget.output_tokens).toLocaleString(getLocale()),
        prompt: run.prompt_version,
      }) + (run.model ? t("agent.draftBy", { model: run.model }) : "")
    : "";
}

function scheduleAgentRefresh() {
  window.clearTimeout(agentPollTimer);
  if (agentRun?.status === "running") {
    agentPollTimer = window.setTimeout(refreshAgentRun, 1000);
  }
}

async function refreshAgentRun() {
  if (!agentRun) return;
  try {
    renderAgentRun(await getAgentRun(agentRun.run_id));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      rememberAgentRun(null);
      renderAgentRun(null);
      return;
    }
    showFeedback(elements.agentFeedback, error.message, true);
  }
  scheduleAgentRefresh();
}

async function restoreAgentRun() {
  const runId = storedAgentRunId();
  if (!runId || agentRun) return;
  agentRun = { run_id: runId, status: "running" };
  await refreshAgentRun();
}

function resetAgentDecisions() {
  agentDecisions = new Map();
  agentHighlightAccepts = new Set();
  agentCheckPointAccepts = new Map();
}

function agentCommitPayload() {
  const claims = [];
  const ordered = [...agentDecisions.entries()].sort((left, right) => left[0] - right[0]);
  for (const [index, decision] of ordered) {
    if (decision.action === "skip") continue;
    if (decision.action === "edit" && !decision.text.trim()) {
      throw new Error(t("agent.editEmpty", { number: index + 1 }));
    }
    if (decision.action === "reject" && !decision.reason.trim()) {
      throw new Error(t("agent.rejectNeedsReason", { number: index + 1 }));
    }
    claims.push({
      claim_index: index,
      action: decision.action,
      ...(decision.action === "edit" ? { text: decision.text.trim() } : {}),
      ...(decision.action === "reject" ? { reason: decision.reason.trim() } : {}),
    });
  }
  const highlights = [...agentHighlightAccepts]
    .sort((left, right) => left - right)
    .map((index) => ({ highlight_index: index, action: "accept" }));
  const checkPoints = [...agentCheckPointAccepts.entries()]
    .sort((left, right) => left[0] - right[0])
    .map(([index, accepted]) => ({
      check_point_index: index,
      action: accepted ? "accept" : "reject",
    }));
  return { claims, highlights, check_points: checkPoints };
}

function renderAgentReviewSection(sections) {
  const review = sections.find((section) => section.section === "agent_review");
  elements.agentLatestReview.hidden = !review;
  if (!review) return;
  elements.agentLatestVersion.textContent = t("agent.latestVersion", { version: review.version });
  elements.agentLatestContent.textContent = review.content;
}

async function runAgentAction(button, action) {
  button.disabled = true;
  try {
    const run = await action();
    if (run.run_id !== agentRun?.run_id) resetAgentDecisions();
    rememberAgentRun(run.run_id);
    button.disabled = false;
    renderAgentRun(run);
    scheduleAgentRefresh();
    return run;
  } catch (error) {
    button.disabled = false;
    if (agentRun?.draft) renderAgentRun(agentRun);
    const message =
      typeof error.message === "string" && !error.message.startsWith("[object")
        ? error.message
        : t("agent.requestRejected");
    showFeedback(elements.agentFeedback, message, true);
    return null;
  }
}


elements.agentStart.addEventListener("click", () => {
  runAgentAction(elements.agentStart, () => startAgentRun());
});
elements.agentRevise.addEventListener("click", () => {
  const instruction = elements.agentReviseInstruction.value.trim();
  if (!instruction) {
    showFeedback(elements.agentFeedback, t("agent.describeRevision"), true);
    return;
  }
  runAgentAction(elements.agentRevise, async () => {
    const run = await reviseAgentRun(agentRun.run_id, instruction);
    elements.agentReviseInstruction.value = "";
    resetAgentDecisions();
    return run;
  });
});
elements.agentCommit.addEventListener("click", () => {
  let payload;
  try {
    payload = agentCommitPayload();
  } catch (error) {
    showFeedback(elements.agentFeedback, error.message, true);
    return;
  }
  runAgentAction(elements.agentCommit, async () => {
    const run = await commitAgentRun(agentRun.run_id, payload);
    await hooks.reloadPatient();
    return run;
  });
});
elements.agentDiscard.addEventListener("click", () => {
  runAgentAction(elements.agentDiscard, () => discardAgentRun(agentRun.run_id));
});

export function initAgentPanel(pageHooks) {
  Object.assign(hooks, pageHooks);
}

export { renderAgentReviewSection, restoreAgentRun };

/** Forget the run on sign-out. */
export function resetAgentPanel() {
  window.clearTimeout(agentPollTimer);
  agentRun = null;
  resetAgentDecisions();
  rememberAgentRun(null);
}

/** Refresh when a realtime event concerns the run on screen. */
export function handleAgentEvent(message) {
  if (message.type?.startsWith("agent.") && message.run_id === agentRun?.run_id) {
    refreshAgentRun();
  }
}

export function rerenderAgentPanel() {
  if (agentRun?.draft || agentRun?.status) renderAgentRun(agentRun);
  else elements.agentRunStatus.textContent = t("agent.noReview");
}
