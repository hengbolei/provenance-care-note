// The reference library: the care team reads documents; administrators add and retire them.
import {
  DEMO_CONTEXT,
  addKnowledgeVersion,
  createKnowledge,
  getKnowledge,
  getKnowledgeVersion,
  listKnowledge,
  retireKnowledge,
} from "/static/api.js";
import { createElement, formatDate, showFeedback } from "/static/dom.js";
import { t } from "/static/i18n.js";

const elements = {
  knowledgeCount: document.querySelector("#knowledge-count"),
  knowledgeShowRetired: document.querySelector("#knowledge-show-retired"),
  knowledgeNew: document.querySelector("#knowledge-new"),
  knowledgeList: document.querySelector("#knowledge-list"),
  knowledgeFeedback: document.querySelector("#knowledge-feedback"),
  knowledgeViewer: document.querySelector("#knowledge-viewer"),
  knowledgeViewerTitle: document.querySelector("#knowledge-viewer-title"),
  knowledgeViewerMeta: document.querySelector("#knowledge-viewer-meta"),
  knowledgeVersionSelect: document.querySelector("#knowledge-version-select"),
  knowledgeContent: document.querySelector("#knowledge-content"),
  knowledgeForm: document.querySelector("#knowledge-form"),
  knowledgeFormTitle: document.querySelector("#knowledge-form-title"),
  knowledgeDocumentFields: document.querySelector("#knowledge-document-fields"),
  knowledgeKind: document.querySelector("#knowledge-kind"),
  knowledgeDocTitle: document.querySelector("#knowledge-doc-title"),
  knowledgeSource: document.querySelector("#knowledge-source"),
  knowledgeLabel: document.querySelector("#knowledge-label"),
  knowledgeEffective: document.querySelector("#knowledge-effective"),
  knowledgeFile: document.querySelector("#knowledge-file"),
  knowledgeText: document.querySelector("#knowledge-text"),
  knowledgeCancel: document.querySelector("#knowledge-cancel"),
};

let knowledgeDocuments = [];
// The document being viewed, and the form's mode: null, { mode: "create" } or
// { mode: "version", doc }.
let knowledgeViewed = null;
let knowledgeEditing = null;

const isAdmin = () => DEMO_CONTEXT.actorRole === "admin";

async function loadKnowledge() {
  try {
    knowledgeDocuments = await listKnowledge(elements.knowledgeShowRetired.checked);
    renderKnowledgeList();
  } catch (error) {
    knowledgeDocuments = [];
    renderKnowledgeList();
    showFeedback(elements.knowledgeFeedback, error.message || t("knowledge.loadFailed"), true);
  }
}

function renderKnowledgeList() {
  elements.knowledgeNew.hidden = !isAdmin();
  elements.knowledgeCount.textContent = t("knowledge.count", { count: knowledgeDocuments.length });
  elements.knowledgeList.replaceChildren();
  if (knowledgeDocuments.length === 0) {
    elements.knowledgeList.append(createElement("p", "helper-text", t("knowledge.empty")));
    return;
  }
  knowledgeDocuments.forEach((doc) => {
    const retired = doc.status === "retired";
    const item = createElement("article", `knowledge-item${retired ? " retired" : ""}`);
    const body = createElement("div");
    const meta = createElement("div", "agent-claim-meta");
    meta.append(createElement("span", "agent-chip", t(`knowledge.kind.${doc.kind}`)));
    if (retired) meta.append(createElement("span", "agent-chip", t("knowledge.status.retired")));
    body.append(
      createElement("p", "knowledge-item-title", doc.title),
      createElement(
        "p",
        "knowledge-item-meta",
        t("knowledge.itemMeta", { version: doc.latest_version, source: doc.source }),
      ),
      meta,
    );
    const actions = createElement("div", "knowledge-actions");
    const view = createElement("button", "text-button", t("knowledge.view"));
    view.type = "button";
    view.addEventListener("click", () => viewKnowledge(doc.id));
    actions.append(view);
    if (isAdmin() && !retired) {
      const version = createElement("button", "text-button", t("knowledge.newVersion"));
      version.type = "button";
      version.addEventListener("click", () => openKnowledgeForm({ mode: "version", doc }));
      const retire = createElement("button", "text-button", t("knowledge.retire"));
      retire.type = "button";
      retire.addEventListener("click", () => retireKnowledgeDocument(doc));
      actions.append(version, retire);
    }
    item.append(body, actions);
    elements.knowledgeList.append(item);
  });
}

async function viewKnowledge(documentId, version) {
  try {
    const detail = await getKnowledge(documentId);
    const chosen = version || detail.current_version || detail.document.latest_version;
    knowledgeViewed = { detail, version: chosen };
    renderKnowledgeViewer();
    const text = await getKnowledgeVersion(documentId, chosen);
    elements.knowledgeContent.textContent = text.content;
    elements.knowledgeViewer.hidden = false;
    showFeedback(elements.knowledgeFeedback, "");
  } catch (error) {
    showFeedback(elements.knowledgeFeedback, error.message || t("knowledge.loadFailed"), true);
  }
}

function renderKnowledgeViewer() {
  if (!knowledgeViewed) return;
  const { detail, version } = knowledgeViewed;
  elements.knowledgeViewerTitle.textContent = detail.document.title;
  elements.knowledgeViewerMeta.textContent = [
    t(`knowledge.kind.${detail.document.kind}`),
    detail.document.source,
    detail.current_version
      ? t("knowledge.currentVersion", { version: detail.current_version })
      : t("knowledge.noCurrentVersion"),
  ].join(" · ");
  elements.knowledgeVersionSelect.replaceChildren(
    ...[...detail.versions].reverse().map((item) => {
      const option = createElement(
        "option",
        "",
        t("knowledge.versionOption", {
          version: item.version,
          label: item.label,
          date: formatDate(item.effective_date),
        }),
      );
      option.value = String(item.version);
      option.selected = item.version === version;
      return option;
    }),
  );
}

function renderKnowledgeFormTitle() {
  if (!knowledgeEditing) return;
  elements.knowledgeFormTitle.textContent =
    knowledgeEditing.mode === "create"
      ? t("knowledge.createTitle")
      : t("knowledge.versionTitle", { title: knowledgeEditing.doc.title });
}

function openKnowledgeForm(editing) {
  knowledgeEditing = editing;
  const creating = editing.mode === "create";
  renderKnowledgeFormTitle();
  elements.knowledgeDocumentFields.hidden = !creating;
  elements.knowledgeDocTitle.value = "";
  elements.knowledgeSource.value = "";
  elements.knowledgeLabel.value = "";
  elements.knowledgeEffective.value = new Date().toISOString().slice(0, 10);
  elements.knowledgeFile.value = "";
  elements.knowledgeText.value = "";
  elements.knowledgeForm.hidden = false;
  showFeedback(elements.knowledgeFeedback, "");
  (creating ? elements.knowledgeDocTitle : elements.knowledgeLabel).focus();
}

function closeKnowledgeForm() {
  knowledgeEditing = null;
  elements.knowledgeForm.hidden = true;
}

async function saveKnowledge(event) {
  event.preventDefault();
  if (!knowledgeEditing) return;
  const fields = {
    version_label: elements.knowledgeLabel.value.trim(),
    effective_date: elements.knowledgeEffective.value,
    content: elements.knowledgeText.value,
  };
  try {
    let detail;
    if (knowledgeEditing.mode === "create") {
      detail = await createKnowledge({
        ...fields,
        kind: elements.knowledgeKind.value,
        title: elements.knowledgeDocTitle.value.trim(),
        source: elements.knowledgeSource.value.trim(),
      });
    } else {
      detail = await addKnowledgeVersion(knowledgeEditing.doc.id, {
        ...fields,
        expected_version: knowledgeEditing.doc.latest_version,
      });
    }
    closeKnowledgeForm();
    await loadKnowledge();
    await viewKnowledge(detail.document.id, detail.document.latest_version);
    showFeedback(elements.knowledgeFeedback, t("knowledge.saved"));
  } catch (error) {
    showFeedback(elements.knowledgeFeedback, error.message || t("knowledge.saveFailed"), true);
  }
}

async function retireKnowledgeDocument(doc) {
  if (!window.confirm(t("knowledge.retireConfirm", { title: doc.title }))) return;
  try {
    await retireKnowledge(doc.id, doc.latest_version);
    if (knowledgeViewed?.detail.document.id === doc.id) {
      knowledgeViewed = null;
      elements.knowledgeViewer.hidden = true;
    }
    await loadKnowledge();
    showFeedback(elements.knowledgeFeedback, t("knowledge.retired"));
  } catch (error) {
    showFeedback(elements.knowledgeFeedback, error.message || t("knowledge.saveFailed"), true);
  }
}

elements.knowledgeShowRetired.addEventListener("change", loadKnowledge);
elements.knowledgeNew.addEventListener("click", () => openKnowledgeForm({ mode: "create" }));
elements.knowledgeCancel.addEventListener("click", closeKnowledgeForm);
elements.knowledgeForm.addEventListener("submit", saveKnowledge);
elements.knowledgeVersionSelect.addEventListener("change", () => {
  if (knowledgeViewed) {
    viewKnowledge(knowledgeViewed.detail.document.id, Number(elements.knowledgeVersionSelect.value));
  }
});
elements.knowledgeFile.addEventListener("change", async () => {
  const [file] = elements.knowledgeFile.files;
  if (file) elements.knowledgeText.value = await file.text();
});

export { loadKnowledge };

export function rerenderKnowledgePanel() {
  renderKnowledgeList();
  renderKnowledgeViewer();
  renderKnowledgeFormTitle();
}
