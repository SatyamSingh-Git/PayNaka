/** Thin client for the PayNaka service. */

export type Verdict = 'ALLOW' | 'DENY' | 'STEP_UP';

export interface GateDecision {
  verdict: Verdict;
  action: string;
  reason: string;
  check_id: string | null;
  evidence: Record<string, unknown>;
  mandate_id: string | null;
  request_id: string | null;
  latency_us: number;
  replayed: boolean;
}

export interface DemoRun {
  session_id: string;
  scenario: 'happy' | 'attack';
  gate: boolean;
  authorised: number;
  authorised_formatted: string;
  money_moved: number;
  money_moved_formatted: string;
  overspent: number;
  overspent_formatted: string;
  poisoned_field: string | null;
  denials: GateDecision[];
  executions: Array<{ decision: GateDecision; money_moved: number; audit_seq: number | null }>;
  transcript: Array<{ role: string; name?: string; text?: string; args?: Record<string, unknown> }>;
  audit_head: string;
  note: string;
}

export interface AuditRecord {
  seq: number;
  ts: number;
  prev_hash: string;
  hash: string;
  payload: Record<string, any>;
}

export interface PolicyView {
  merchant: string;
  require_idempotency_key: boolean;
  step_up_timeout_seconds: number;
  on_step_up_timeout: string;
  actions: Record<
    string,
    {
      enabled: boolean;
      max_amount: number | null;
      max_amount_formatted: string | null;
      step_up_above: number | null;
      step_up_above_formatted: string | null;
      daily_cap: number | null;
      daily_cap_formatted: string | null;
      require_return_event: boolean;
    }
  >;
  regulatory: {
    npci_mandate_retries: number;
    debit_blackout: string[];
    contact_window: string | null;
    afa_threshold: number | null;
    afa_threshold_formatted: string | null;
    pre_debit_notice_seconds: number;
  };
}

export interface Health {
  status: string;
  rail: string;
  env: string;
  test_mode: boolean;
  merchant: string;
  audit_records: number;
  mode: 'enforce' | 'observe';
  enforcing: boolean;
}

/** A money action waiting for a person. Approving releases exactly one request, once. */
export interface Escalation {
  id: string;
  request_hash: string;
  session_id: string;
  subject: string;
  action: string;
  amount: number;
  amount_formatted: string;
  summary: Record<string, unknown>;
  created_at: number;
  expires_at: number;
  state: 'pending' | 'approved' | 'denied' | 'consumed';
  decided_by: string | null;
}

export interface EscalationQueue {
  timeout_seconds: number;
  on_timeout: string;
  approvers_configured: number;
  pending: Escalation[];
  expired: Escalation[];
}

/** What enforcement would have changed. Every number zero unless the mode is observe. */
export interface ShadowReport {
  mode: 'enforce' | 'observe';
  enforcing: boolean;
  decisions: number;
  observed: number;
  money_at_risk: number;
  money_at_risk_formatted: string;
  rate: number;
  top_check: string | null;
  by_check: Record<string, number>;
  by_check_amount_formatted: Record<string, string>;
}

export interface Metrics {
  decisions: number;
  allowed: number;
  denied: number;
  stepped_up: number;
  replayed: number;
  executed: number;
  money_moved: number;
  money_moved_formatted: string;
  by_check: Record<string, number>;
  breaker_trips: number;
  escalations_opened: number;
  escalations_approved: number;
  escalations_denied: number;
  observed_suppressions: number;
  chain_records: number;
  chain_intact: boolean;
  mode: 'enforce' | 'observe';
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}

async function post<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { method: 'POST', ...init });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}

/**
 * The approver credential, which is deliberately NOT the agent's.
 *
 * A step-up the buying agent can answer on its own behalf is theatre, so the service keeps
 * two separate credential sets and refuses to start if a token appears in both. `make dev`
 * passes the development one it minted; without it the approve button gets an honest 401
 * rather than a silent success.
 */
const APPROVER_TOKEN = (import.meta.env.VITE_PAYNAKA_APPROVER_TOKEN as string | undefined) ?? '';

const approverHeaders = (): HeadersInit =>
  APPROVER_TOKEN ? { Authorization: `Bearer ${APPROVER_TOKEN}` } : {};

export const api = {
  health: () => get<Health>('/api/health'),
  policy: () => get<PolicyView>('/api/policy'),
  audit: (since = 0) =>
    get<{ head: string; count: number; records: AuditRecord[] }>(`/api/audit?since=${since}`),
  verifyAudit: () =>
    get<{ intact: boolean; records: number; head: string; break: unknown }>('/api/audit/verify'),
  runDemo: (scenario: 'happy' | 'attack', gate: boolean) =>
    post<DemoRun>(`/api/demo/${scenario}?gate=${gate}`),
  escalations: () => get<EscalationQueue>('/api/escalations'),
  decide: (id: string, answer: 'approve' | 'deny') =>
    post<Escalation>(`/api/escalations/${id}/${answer}`, { headers: approverHeaders() }),
  shadow: () => get<ShadowReport>('/api/shadow'),
  metrics: () => get<Metrics>('/api/metrics'),
  hasApproverCredential: () => APPROVER_TOKEN.length > 0,
};

/** Paise -> "₹1,999.00", with Indian lakh/crore grouping. */
export function formatInr(paise: number): string {
  const sign = paise < 0 ? '-' : '';
  const whole = Math.floor(Math.abs(paise) / 100);
  const frac = String(Math.abs(paise) % 100).padStart(2, '0');

  let digits = String(whole);
  if (digits.length > 3) {
    const tail = digits.slice(-3);
    let head = digits.slice(0, -3);
    const groups: string[] = [];
    while (head.length > 2) {
      groups.unshift(head.slice(-2));
      head = head.slice(0, -2);
    }
    if (head) groups.unshift(head);
    digits = [...groups, tail].join(',');
  }
  return `${sign}₹${digits}.${frac}`;
}
