/**
 * Every screen, mounted against fixture data, asserting it renders something.
 *
 * This exists because of a bug that shipped past two green checks. `Amount` defaults to
 * `type="body"`, whose sizes stop at `large`; I passed `size="2xlarge"`, which exists only
 * on the heading variant. TypeScript accepted it — the three variants form a union and every
 * `type` is optional, so the props matched `AmountHeadingProps` structurally — while React
 * defaulted to `body` and Blade went looking for a token that is not there.
 *
 * `tsc --noEmit` was clean. `vite build` was clean. The Operations screen rendered a blank
 * page, and the only reason anybody found out is that a human opened a browser.
 *
 * Live would have gone the same way and *later*: its hero only renders an `Amount` once a
 * scenario has been run, so it looked correct right up until somebody pressed the button in
 * front of an audience.
 *
 * So the bar here is deliberately low and the coverage deliberately total. These tests do
 * not check layout, copy, or design — they check that mounting the component does not
 * throw, which is exactly the class of failure a type-checker cannot see and a screenshot
 * catches only if somebody happens to look at that screen that day.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { BladeProvider } from '@razorpay/blade/components';
import { bladeTheme } from '@razorpay/blade/tokens';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The real files `make console-data` writes, imported rather than hand-written. A fixture
// I invent is a fixture that drifts from the shape the screen actually receives -- these
// cannot, and if the generator changes its output the test follows it.
import chaosData from '../../public/chaos.json';
import sentinelData from '../../public/sentinel.json';
import toctouData from '../../public/toctou.json';
import { App } from '../App';
import { Benchmark } from './Benchmark';
import { Live } from './Live';
import { Operations } from './Operations';
import { Policy } from './Policy';
import { Replay } from './Replay';

// ------------------------------------------------------------------ fixtures
// Shaped like the real responses, with the values that exercise the risky paths: money
// large enough to format with grouping, a pending escalation so the table renders rather
// than the empty state, and a broken chain so the alarm path is mounted too.

const HEALTH = {
  status: 'ok',
  rail: 'sim',
  env: 'sandbox',
  test_mode: true,
  merchant: 'kirana-co',
  audit_records: 3,
  mode: 'enforce',
  enforcing: true,
  durable: false,
};

const ESCALATIONS = {
  timeout_seconds: 300,
  on_timeout: 'DENY',
  approvers_configured: 1,
  pending: [
    {
      id: 'esc_1',
      request_hash: 'abc123',
      session_id: 'sess_demo',
      subject: 'cust_1',
      action: 'create_order',
      amount: 5_199_900,
      amount_formatted: '₹51,999.00',
      summary: {},
      created_at: 1,
      expires_at: 2,
      state: 'pending',
      decided_by: null,
    },
  ],
  expired: [],
};

const SHADOW = {
  mode: 'enforce',
  enforcing: true,
  decisions: 12,
  observed: 0,
  money_at_risk: 0,
  money_at_risk_formatted: '₹0.00',
  rate: 0,
  top_check: null,
  by_check: {},
  by_check_amount_formatted: {},
};

const METRICS = {
  decisions: 12,
  allowed: 8,
  denied: 3,
  stepped_up: 1,
  replayed: 2,
  executed: 8,
  money_moved: 5_199_900,
  money_moved_formatted: '₹51,999.00',
  by_check: { 'envelope.item_not_in_intent': 3 },
  breaker_trips: 0,
  escalations_opened: 1,
  escalations_approved: 0,
  escalations_denied: 0,
  observed_suppressions: 0,
  chain_records: 3,
  chain_intact: true,
  mode: 'enforce',
};

const AUDIT = {
  head: 'deadbeefdeadbeef',
  count: 1,
  records: [
    {
      seq: 1,
      ts: 1787587007,
      hash: 'deadbeefdeadbeef',
      prev_hash: '0'.repeat(16),
      payload: { kind: 'decision', verdict: 'DENY', action: 'create_order' },
    },
  ],
};

const POLICY = {
  merchant: 'kirana-co',
  step_up_timeout_seconds: 300,
  on_step_up_timeout: 'DENY',
  actions: { create_order: { max_amount: 500_000, step_up_above: 200_000 } },
  regulatory: {
    npci_mandate_retries: 3,
    debit_blackout: ['10:00-13:00'],
    contact_window: '08:00-19:00',
    afa_threshold: 1_500_000,
    pre_debit_notice_seconds: 86_400,
  },
};

/** Everything the screens fetch, keyed by the tail of the path. */
const ROUTES: Record<string, unknown> = {
  '/api/health': HEALTH,
  '/api/escalations': ESCALATIONS,
  '/api/shadow': SHADOW,
  '/api/metrics': METRICS,
  '/api/policy': POLICY,
  '/api/audit/verify': { intact: true, records: 3, head: 'deadbeefdeadbeef', break: null },
  // The console's own data files, exactly as committed.
  '/chaos.json': chaosData,
  '/sentinel.json': sentinelData,
  '/toctou.json': toctouData,
};

function respond(url: string): unknown {
  if (url.startsWith('/api/audit')) return AUDIT;
  const hit = Object.keys(ROUTES).find((route) => url.startsWith(route));
  return hit ? ROUTES[hit] : {};
}

function mount(node: React.ReactNode) {
  return render(
    <BladeProvider themeTokens={bladeTheme} colorScheme="light">
      {node}
    </BladeProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) =>
      Promise.resolve({
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => respond(String(input)),
      } as Response),
    ),
  );
});

afterEach(() => {
  // Explicit, because `globals: false` means Testing Library's automatic cleanup never
  // registers. Without it every render stacks up in the same document and the next test
  // fails on "found multiple elements" -- which looks like a component bug and is not one.
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ------------------------------------------------------------------ the tests

describe('every screen mounts', () => {
  const SCREENS = [
    ['Live', <Live key="live" />],
    ['Operations', <Operations key="ops" />],
    ['Benchmark', <Benchmark key="bench" />],
    ['Replay', <Replay key="replay" />],
    ['Policy', <Policy key="policy" />],
  ] as const;

  it.each(SCREENS)('%s renders without throwing', async (_name, node) => {
    const { container } = mount(node);
    // Waits, because every screen fetches on mount and the crash we are guarding against
    // happens on the *second* render — the one with data in it. Asserting synchronously
    // would only ever prove the spinner works.
    await waitFor(() => expect(container.textContent?.length ?? 0).toBeGreaterThan(0));
    expect(container.querySelector('*')).not.toBeNull();
  });

  it('the shell renders with its navigation', async () => {
    mount(<App />);
    await waitFor(() => expect(screen.getByText('PayNaka')).toBeTruthy());
  });
});

describe('the paths that only render once data arrives', () => {
  it('Operations shows a pending escalation, not the empty state', async () => {
    mount(<Operations />);
    // This is the render that used to throw: the table, the Amount, and the money tiles
    // all appear only after the fetch resolves.
    await waitFor(() => expect(screen.getByText('create_order')).toBeTruthy());
    expect(screen.getByText('1 pending')).toBeTruthy();
  });

  it('Operations renders its money tiles', async () => {
    mount(<Operations />);
    await waitFor(() => expect(screen.getByText(/Money moved/i)).toBeTruthy());
  });

  it('Operations raises the alarm when the chain is broken', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const body = url.startsWith('/api/metrics')
          ? { ...METRICS, chain_intact: false }
          : respond(url);
        return { ok: true, status: 200, statusText: 'OK', json: async () => body } as Response;
      }),
    );
    mount(<Operations />);
    await waitFor(() => expect(screen.getByText(/does not verify/i)).toBeTruthy());
  });

  it('Live renders its hero before anything has been run', async () => {
    mount(<Live />);
    await waitFor(() => expect(screen.getByText(/WITHOUT PAYNAKA/i)).toBeTruthy());
    expect(screen.getByText(/WITH PAYNAKA/i)).toBeTruthy();
  });
});

describe('a screen survives the service being down', () => {
  it.each([
    ['Live', <Live key="live" />],
    ['Operations', <Operations key="ops" />],
    ['Policy', <Policy key="policy" />],
  ] as const)('%s does not throw when every fetch fails', async (_name, node) => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new Error('connection refused'))));
    const { container } = mount(node);
    // An operator whose service is down should see a message, not a white page.
    await waitFor(() => expect(container.textContent?.length ?? 0).toBeGreaterThan(0));
  });
});
