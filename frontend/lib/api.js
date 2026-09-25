import axios from "axios";

const api = axios.create({
  baseURL: "/api",
  timeout: 15000,
  headers: { "Content-Type": "application/json" },
});

// ── Dashboard ─────────────────────────────────────────────────────────────

export async function fetchStats() {
  const res = await api.get("/dashboard/stats");
  return res.data;
}

export async function fetchCalls(page = 1, limit = 20, status = "") {
  const params = { page, limit };
  if (status) params.status = status;
  const res = await api.get("/dashboard/calls", { params });
  return res.data;
}

export async function fetchTranscript(callId) {
  const res = await api.get(`/dashboard/calls/${callId}/transcript`);
  return res.data;
}

export async function fetchLeads(page = 1, limit = 20) {
  const res = await api.get("/dashboard/leads", { params: { page, limit } });
  return res.data;
}

export async function fetchMeetings(page = 1, limit = 20) {
  const res = await api.get("/dashboard/meetings", { params: { page, limit } });
  return res.data;
}

export async function fetchEscalations(resolved = null) {
  const params = {};
  if (resolved !== null) params.resolved = resolved;
  const res = await api.get("/dashboard/escalations", { params });
  return res.data;
}

export async function resolveEscalation(id) {
  const res = await api.post(`/dashboard/escalations/${id}/resolve`);
  return res.data;
}

// ── Settings ──────────────────────────────────────────────────────────────

export async function fetchSettings() {
  const res = await api.get("/settings");
  return res.data;
}

export async function updateSetting(key, value) {
  const res = await api.post("/settings", { key, value });
  return res.data;
}

// ── Documents (v2 — knowledge base upload) ─────────────────────────────────

export async function fetchDocuments() {
  const res = await api.get("/documents");
  return res.data;
}

export async function uploadDocument(file) {
  const formData = new FormData();
  formData.append("file", file);
  const res = await api.post("/documents/upload", formData, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 60000,
  });
  return res.data;
}

export async function deleteDocument(source) {
  const res = await api.delete(`/documents/${encodeURIComponent(source)}`);
  return res.data;
}

// ── Analytics (v3+ — sentiment reports + live event feed) ──────────────────

export async function fetchSentimentSummary() {
  const res = await api.get("/analytics/sentiment/summary");
  return res.data;
}

export async function fetchSentimentReports(page = 1, limit = 20) {
  const res = await api.get("/analytics/sentiment", { params: { page, limit } });
  return res.data;
}

export async function fetchRecentEvents(count = 50) {
  const res = await api.get("/analytics/events", { params: { count } });
  return res.data;
}

export async function fetchSystemHealth() {
  const res = await api.get("/analytics/system-health");
  return res.data;
}

// ── Helpers ───────────────────────────────────────────────────────────────

export function exportLeadsCSV(leads) {
  const headers = ["ID", "Name", "Phone", "Email", "Interest", "Date"];
  const rows = leads.map((l) => [
    l.id,
    l.name,
    l.phone,
    l.email || "",
    l.interest || "",
    l.created_at ? new Date(l.created_at).toLocaleDateString() : "",
  ]);
  const csv = [headers, ...rows].map((r) => r.join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "technozis_leads.csv";
  a.click();
  URL.revokeObjectURL(url);
}
// ── Mock interview (browser) ───────────────────────────────────────────────
// Longer timeouts than the default 15s: writing questions and the final
// report are full model calls, and nothing is on a phone line waiting.

export async function startInterview({ role, level, questionCount, focus }) {
  const res = await api.post(
    "/interview/sessions",
    { role, level, question_count: questionCount, focus },
    { timeout: 90000 },
  );
  return res.data;
}

export async function sendInterviewAnswer(sessionId, text) {
  const res = await api.post(`/interview/sessions/${sessionId}/answer`, { text }, { timeout: 60000 });
  return res.data;
}

export async function skipInterviewQuestion(sessionId) {
  const res = await api.post(`/interview/sessions/${sessionId}/skip`);
  return res.data;
}

export async function endInterview(sessionId) {
  const res = await api.post(`/interview/sessions/${sessionId}/end`);
  return res.data;
}

export async function fetchInterviewReport(sessionId, refresh = false) {
  const res = await api.post(`/interview/sessions/${sessionId}/report`, null, {
    params: refresh ? { refresh: true } : {},
    timeout: 120000,
  });
  return res.data;
}

// Returns an audio Blob in the agent's configured voice (Settings page).
export async function synthesizeInterviewSpeech(text) {
  const res = await api.post("/interview/speak", { text }, { responseType: "blob", timeout: 30000 });
  return res.data;
}
