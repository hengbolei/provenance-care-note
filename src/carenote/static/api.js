export const DEMO_CONTEXT = {
  patientId: "20000000-0000-4000-8000-000000000001",
  actorRole: null,
};

export async function login(username, password) {
  const session = await request("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  DEMO_CONTEXT.actorRole = session.actor.role;
  return session;
}

export async function getSession() {
  const session = await request("/api/auth/me");
  DEMO_CONTEXT.actorRole = session.actor.role;
  return session;
}

export async function logout() {
  await request("/api/auth/logout", { method: "POST", expectEmpty: true });
  DEMO_CONTEXT.actorRole = null;
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function getPatientDetail(context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}`, {}, context);
}

export function getEntrySource(entryId, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/entries/${entryId}/source`, {}, context);
}

export function getHighlightSource(highlightId, context = DEMO_CONTEXT) {
  return request(
    `/api/patients/${context.patientId}/highlights/${highlightId}/source`,
    {},
    context,
  );
}

async function request(path, options = {}, context = DEMO_CONTEXT) {
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers: {
      ...(options.body ? { "content-type": "application/json" } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let message = "Unable to load the patient record";
    try {
      const payload = await response.json();
      message = payload.detail || message;
    } catch {
      // Keep the safe generic message when the server response is not JSON.
    }
    throw new ApiError(message, response.status);
  }
  if (options.expectEmpty) return null;
  return response.json();
}

export function addComment(
  content,
  assignedTo,
  parentId = null,
  target = null,
  context = DEMO_CONTEXT,
) {
  return request(`/api/patients/${context.patientId}/comments`, {
    method: "POST",
    body: JSON.stringify({
      content,
      assigned_to: assignedTo || null,
      parent_id: parentId,
      target,
    }),
  }, context);
}

export function setCommentResolved(commentId, resolved, expectedVersion, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/comments/${commentId}`, {
    method: "PATCH",
    body: JSON.stringify({ resolved, expected_version: expectedVersion }),
  }, context);
}

export function getHighlightRevisions(highlightId, context = DEMO_CONTEXT) {
  return request(
    `/api/patients/${context.patientId}/highlights/${highlightId}/revisions`,
    {},
    context,
  );
}

export function getCommentRevisions(commentId, context = DEMO_CONTEXT) {
  return request(
    `/api/patients/${context.patientId}/comments/${commentId}/revisions`,
    {},
    context,
  );
}

export function updateConflict(conflictId, changes, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/conflicts/${conflictId}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  }, context);
}

export function getConflictRevisions(conflictId, context = DEMO_CONTEXT) {
  return request(
    `/api/patients/${context.patientId}/conflicts/${conflictId}/revisions`,
    {},
    context,
  );
}

export function updateHighlight(highlightId, changes, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/highlights/${highlightId}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  }, context);
}

export function addTimelineEntry(title, content, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/entries`, {
    method: "POST",
    body: JSON.stringify({ title, content }),
  }, context);
}

export function createHighlight(payload, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/highlights`, {
    method: "POST",
    body: JSON.stringify(payload),
  }, context);
}

export function recordHighlightImpression(highlightId, expectedVersion, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/highlights/${highlightId}/impressions`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion }),
    expectEmpty: true,
  }, context);
}

export function ingestAI(title, rawText, source, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/ai-ingest`, {
    method: "POST",
    body: JSON.stringify({ title, raw_text: rawText, source }),
  }, context);
}

export async function transcribeAudio(blob, context = DEMO_CONTEXT) {
  const response = await fetch(`/api/patients/${context.patientId}/transcriptions`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": blob.type || "audio/webm" },
    body: blob,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new ApiError(payload.detail || "Unable to transcribe recording", response.status);
  }
  return response.json();
}

export function getAuditEvents(context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/audit`, {}, context);
}

export function updateSection(section, content, expectedVersion, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/sections/${section}`, {
    method: "PUT",
    body: JSON.stringify({ content, expected_version: expectedVersion }),
  }, context);
}

export function getSectionRevisions(section, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/sections/${section}/revisions`, {}, context);
}

export function compareSectionRevisions(
  section,
  fromVersion,
  toVersion,
  context = DEMO_CONTEXT,
) {
  const query = new URLSearchParams({
    from_version: String(fromVersion),
    to_version: String(toVersion),
  });
  return request(
    `/api/patients/${context.patientId}/sections/${section}/compare?${query}`,
    {},
    context,
  );
}

export function revertSection(section, targetVersion, context = DEMO_CONTEXT) {
  return request(`/api/patients/${context.patientId}/sections/${section}/revert`, {
    method: "POST",
    body: JSON.stringify({ target_version: targetVersion }),
  }, context);
}
