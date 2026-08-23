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
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}

async function post<T>(path: string): Promise<T> {
  const response = await fetch(path, { method: 'POST' });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}

export const api = {
  health: () => get<Health>('/api/health'),
  policy: () => get<PolicyView>('/api/policy'),
  audit: (since = 0) =>
    get<{ head: string; count: number; records: AuditRecord[] }>(`/api/audit?since=${since}`),
  verifyAudit: () =>
    get<{ intact: boolean; records: number; head: string; break: unknown }>('/api/audit/verify'),
  runDemo: (scenario: 'happy' | 'attack', gate: boolean) =>
    post<DemoRun>(`/api/demo/${scenario}?gate=${gate}`),
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
