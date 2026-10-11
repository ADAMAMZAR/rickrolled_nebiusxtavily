// The REST API under /api (app/main.py). Everything in these shapes that came from a message, an email or a web
// page is untrusted text: React renders it as text, and only http(s) links become clickable (see safeHref).

export type LoopKind = "task" | "waiting" | "commitment";
export type ActivityBy = "user" | "chat" | "email" | "web" | "check";

export interface Loop {
  id: string;
  goal_id: string | null;
  title: string;
  summary: string;
  kind: LoopKind;
  waiting_on: string | null;
  due: string | null; // YYYY-MM-DD, the user's local date
  next_action: string | null;
  snoozed_until: string | null;
  person_id: string | null;
  watch_query: string | null;
  watch_checked_on: string | null;
  status: "open" | "resolved";
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  goal_title: string | null;
  goal_status: "active" | "done" | null;
  person_name: string | null;
  person_email: string | null;
  resolved_by: ActivityBy | null;
  risk_level?: RiskLevel | null; // latest message check of this loop (from /api/loops only)
}

export interface Source {
  kind: string; // "chat", "email" or "investigation"
  text: string;
  url: string | null;
  sender: string | null;
  created_at: string;
}

export type LoopDetail = Loop & { source: Source };

export interface Attention {
  loop: Loop;
  reason: string;
}

export interface Activity {
  id: string;
  action: string;
  by: ActivityBy;
  detail: string;
  created_at: string;
  can_undo: boolean;
}

export interface PendingAction {
  id: string;
  summary: string;
  payload: { source_url?: string };
}

export interface GoogleStatus {
  connected: boolean;
  set_up: boolean;
  last_sync: string | null;
}

export type RiskLevel = "LOW" | "GUARDED" | "ELEVATED" | "HIGH" | "CRITICAL" | "INSUFFICIENT_EVIDENCE";
export type Tier = "A" | "B" | "C" | "D" | "E";
export type Direction = "supports" | "contradicts" | "warns" | "neutral";

export interface Evidence {
  id: string;
  claim_id: string | null;
  entity_id: string | null;
  url: string;
  host: string;
  tier: Tier;
  direction: Direction;
  quote: string;
  official_type: string | null;
  official_value: string | null;
}

export interface Signal {
  id: string;
  kind: string;
  weight: number;
  evidence_id: string | null;
  input_quote: string | null;
  reason: string;
}

export interface GraphNode {
  id: string;
  type: string;
  label: string;
  flag?: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  kind: string;
  evidence_id: string | null;
}

export interface Investigation {
  id: string;
  status: "running" | "done" | "failed";
  input_text: string;
  input_url: string | null;
  risk_level: RiskLevel | null;
  score: number | null;
  confidence: "LOW" | "MEDIUM" | "HIGH" | null;
  identity: "verified" | "mismatch" | "unverified" | null;
  findings: { text: string; evidence_ids: string[]; signal_ids: string[] }[];
  next_steps: string[];
  steps: { at: string; text: string }[];
  error: string | null;
  loop_id: string | null;
  loop_title: string | null;
  created_at: string;
  updated_at: string;
  entities: { id: string; type: string; value: string }[];
  claims: { id: string; text: string; category: string; verdict: string; reason: string }[];
  evidence: Evidence[];
  signals: Signal[];
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
}

/** JSON request to /api. Throws an Error carrying the API's own message. */
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (res.status === 204) return null as T;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.message || `Request failed (HTTP ${res.status}).`);
  return body as T;
}

export const send = <T>(path: string, method: string, body?: unknown) =>
  api<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

/** Starts a message check (multipart, so a screenshot can ride along). Returns the new check's id. */
export async function startCheck(form: FormData): Promise<string> {
  const res = await fetch("/api/investigations", { method: "POST", body: form });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.message || "Couldn't start the check.");
  return body.id;
}

/** Only http(s) links from untrusted text become clickable. */
export const safeHref = (url: string | null | undefined) => (url && /^https?:\/\//i.test(url) ? url : undefined);
