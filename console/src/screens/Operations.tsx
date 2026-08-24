/**
 * Operations: the approval queue, what the checkpoint would have stopped, and the one
 * metric worth an alarm.
 *
 * Three surfaces that all answer the same operator question -- *is this thing working, and
 * what is it doing to my traffic* -- and which had working APIs and no way to see them.
 *
 * The approval queue is the part worth demonstrating. Money pauses mid-flight above the
 * step-up band, a person decides, and only then does it move. Nobody decides, and it dies
 * closed: `on_timeout` is DENY and not configurable.
 */

import React from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Card,
  CardBody,
  Divider,
  Heading,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableHeaderRow,
  TableRow,
  Text,
} from '@razorpay/blade/components';
import { api, formatInr, type Escalation, type EscalationQueue, type Metrics, type ShadowReport } from '../api';

/** A small labelled number. The dashboard tile, without pretending to be a chart. */
function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone?: 'positive' | 'negative' | 'notice';
  hint?: string;
}): JSX.Element {
  const colour =
    tone === 'positive'
      ? 'feedback.text.positive.intense'
      : tone === 'negative'
        ? 'feedback.text.negative.intense'
        : tone === 'notice'
          ? 'feedback.text.notice.intense'
          : 'surface.text.gray.normal';
  return (
    <Box minWidth="160px">
      <Text size="xsmall" color="surface.text.gray.muted">
        {label}
      </Text>
      <Heading size="medium" color={colour as never}>
        {value}
      </Heading>
      {hint ? (
        <Text size="xsmall" color="surface.text.gray.muted">
          {hint}
        </Text>
      ) : null}
    </Box>
  );
}

export function Operations(): JSX.Element {
  const [queue, setQueue] = React.useState<EscalationQueue | null>(null);
  const [shadow, setShadow] = React.useState<ShadowReport | null>(null);
  const [metrics, setMetrics] = React.useState<Metrics | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const refresh = React.useCallback(async () => {
    try {
      const [q, s, m] = await Promise.all([api.escalations(), api.shadow(), api.metrics()]);
      setQueue(q);
      setShadow(s);
      setMetrics(m);
      setError(null);
    } catch (cause) {
      setError(String(cause));
    }
  }, []);

  React.useEffect(() => {
    void refresh();
    // Polled rather than streamed: an approval window is minutes long, and a second of
    // staleness on an operator screen is not worth another transport.
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const decide = async (item: Escalation, answer: 'approve' | 'deny') => {
    setBusy(item.id);
    try {
      await api.decide(item.id, answer);
      await refresh();
      setError(null);
    } catch (cause) {
      // A 401 here is the honest outcome when no approver credential is configured, and
      // saying so beats a button that appears to do nothing.
      setError(
        api.hasApproverCredential()
          ? String(cause)
          : `${cause} — no approver credential is configured. A step-up the agent can answer for itself is not an escalation, so the console needs its own.`,
      );
    } finally {
      setBusy(null);
    }
  };

  if (!queue || !metrics || !shadow) {
    return (
      <Box display="flex" gap="spacing.4" alignItems="center">
        <Spinner accessibilityLabel="Loading operations" />
        <Text>Reading the checkpoint…</Text>
      </Box>
    );
  }

  return (
    <Box display="flex" flexDirection="column" gap="spacing.7">
      {error ? <Alert color="negative" description={error} isDismissible={false} /> : null}

      {/* ---------------------------------------------------------------- mode */}
      {!metrics.chain_intact ? (
        <Alert
          color="negative"
          title="The audit chain does not verify"
          description="This is corruption or somebody editing history. Both are incidents."
          isDismissible={false}
        />
      ) : null}

      {shadow.mode === 'observe' ? (
        <Alert
          color="notice"
          title="Observing — nothing is being stopped"
          description="Every check runs and every decision is recorded, and refusals are not acted on. This is what a shadow deployment looks like; it is not a defence while it says this."
          isDismissible={false}
        />
      ) : null}

      {/* ---------------------------------------------------------------- approvals */}
      <Box>
        <Box display="flex" alignItems="center" gap="spacing.4" marginBottom="spacing.4">
          <Heading size="large">Waiting for a person</Heading>
          <Badge color={queue.pending.length ? 'notice' : 'neutral'} emphasis="subtle">
            {queue.pending.length} pending
          </Badge>
          <Badge color="neutral" emphasis="subtle">
            {queue.on_timeout} after {queue.timeout_seconds}s
          </Badge>
        </Box>

        <Text size="small" color="surface.text.gray.muted" marginBottom="spacing.4">
          An approval releases <b>exactly one request, exactly once</b>, and only until the window
          closes. It is bound to the request&rsquo;s hash, so &ldquo;yes to ₹3,500&rdquo; is not
          &ldquo;yes to ₹3,500-ish&rdquo;. Nobody answers and it resolves to {queue.on_timeout} —
          that is not configurable.
        </Text>

        {queue.approvers_configured === 0 ? (
          <Alert
            color="notice"
            description="No approver credential is configured, so nothing here can be approved and every step-up will time out. That is the fail-closed direction."
            isDismissible={false}
            marginBottom="spacing.4"
          />
        ) : null}

        {queue.pending.length === 0 ? (
          <Card>
            <CardBody>
              <Text color="surface.text.gray.muted">
                Nothing is waiting. A step-up appears here when an action goes above the
                auto-approval band in the merchant&rsquo;s policy.
              </Text>
            </CardBody>
          </Card>
        ) : (
          <Table data={{ nodes: queue.pending }}>
            {(tableData) => (
              <>
                <TableHeader>
                  <TableHeaderRow>
                    <TableHeaderCell>Action</TableHeaderCell>
                    <TableHeaderCell>Amount</TableHeaderCell>
                    <TableHeaderCell>Session</TableHeaderCell>
                    <TableHeaderCell>Decide</TableHeaderCell>
                  </TableHeaderRow>
                </TableHeader>
                <TableBody>
                  {tableData.map((item) => (
                    <TableRow key={item.id} item={item}>
                      <TableCell>
                        <Text weight="semibold">{item.action}</Text>
                        <Text size="xsmall" color="surface.text.gray.muted">
                          {item.id}
                        </Text>
                      </TableCell>
                      <TableCell>
                        <Text weight="semibold">{item.amount_formatted}</Text>
                      </TableCell>
                      <TableCell>
                        <Text size="small">{item.session_id}</Text>
                      </TableCell>
                      <TableCell>
                        <Box display="flex" gap="spacing.3">
                          <Button
                            size="xsmall"
                            variant="primary"
                            isLoading={busy === item.id}
                            onClick={() => void decide(item, 'approve')}
                          >
                            Approve
                          </Button>
                          <Button
                            size="xsmall"
                            variant="secondary"
                            color="negative"
                            isDisabled={busy === item.id}
                            onClick={() => void decide(item, 'deny')}
                          >
                            Deny
                          </Button>
                        </Box>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </>
            )}
          </Table>
        )}

        {queue.expired.length > 0 ? (
          <Box marginTop="spacing.4">
            <Text size="small" color="surface.text.gray.muted">
              {queue.expired.length} ran out of time and resolved to {queue.on_timeout}. They are
              shown rather than deleted: &ldquo;nobody answered&rdquo; is a number worth watching.
            </Text>
          </Box>
        ) : null}
      </Box>

      <Divider />

      {/* ---------------------------------------------------------------- shadow */}
      <Box>
        <Heading size="large" marginBottom="spacing.4">
          What enforcement would have changed
        </Heading>
        <Text size="small" color="surface.text.gray.muted" marginBottom="spacing.4">
          Counted from the audit chain rather than a running tally, so it cannot drift from what
          the chain says. Every number is zero while the mode is <b>enforce</b>, and a zeroed
          report is the right answer to &ldquo;what did you let through&rdquo;: nothing.
        </Text>
        <Box display="flex" gap="spacing.7" flexWrap="wrap">
          <Stat label="Mode" value={shadow.mode} tone={shadow.enforcing ? 'positive' : 'notice'} />
          <Stat label="Decisions" value={String(shadow.decisions)} />
          <Stat
            label="Would have been stopped"
            value={String(shadow.observed)}
            tone={shadow.observed ? 'notice' : undefined}
            hint={`${(shadow.rate * 100).toFixed(2)}% of decisions`}
          />
          <Stat
            label="Money at risk"
            value={shadow.money_at_risk_formatted}
            tone={shadow.money_at_risk ? 'notice' : undefined}
            hint="not money saved — money that would not have moved"
          />
          <Stat label="Top check" value={shadow.top_check ?? '—'} />
        </Box>
      </Box>

      <Divider />

      {/* ---------------------------------------------------------------- metrics */}
      <Box>
        <Heading size="large" marginBottom="spacing.4">
          What has happened
        </Heading>
        <Box display="flex" gap="spacing.7" flexWrap="wrap">
          <Stat
            label="Chain intact"
            value={metrics.chain_intact ? 'yes' : 'NO'}
            tone={metrics.chain_intact ? 'positive' : 'negative'}
            hint={`${metrics.chain_records} records`}
          />
          <Stat label="Decisions" value={String(metrics.decisions)} />
          <Stat label="Allowed" value={String(metrics.allowed)} tone="positive" />
          <Stat label="Denied" value={String(metrics.denied)} tone="negative" />
          <Stat label="Sent to a human" value={String(metrics.stepped_up)} tone="notice" />
          <Stat label="Replayed" value={String(metrics.replayed)} hint="duplicates, not charges" />
          <Stat label="Money moved" value={formatInr(metrics.money_moved)} />
          <Stat
            label="Breaker trips"
            value={String(metrics.breaker_trips)}
            tone={metrics.breaker_trips ? 'notice' : undefined}
          />
        </Box>
      </Box>
    </Box>
  );
}
