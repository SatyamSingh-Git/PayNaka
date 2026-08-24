/**
 * Operations: the approval queue, what the checkpoint would have stopped, and the numbers
 * worth an alarm.
 *
 * Rewritten after the first version looked like documentation with a border. The lesson
 * was not subtle: Razorpay's own surfaces are enormous headlines, white cards on a pale
 * ground, and almost no words. Every paragraph of explanation here was a paragraph nobody
 * reads on a projector, so the prose is gone and the numbers are the size of the claim.
 *
 * Money renders through Blade's `Amount`, which is Razorpay's own component and knows the
 * locale's grouping. Hand-formatting rupees was the thing making this look foreign.
 */

import React from "react";
import {
  Alert,
  Amount,
  Badge,
  Box,
  Button,
  Card,
  CardBody,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableHeaderRow,
  TableRow,
  Text,
} from "@razorpay/blade/components";
import {
  api,
  type Escalation,
  type EscalationQueue,
  type Metrics,
  type ShadowReport,
} from "../api";
import { PageHeader, Section, Stat } from "../ui";

export function Operations(): JSX.Element {
  const [queue, setQueue] = React.useState<EscalationQueue | null>(null);
  const [shadow, setShadow] = React.useState<ShadowReport | null>(null);
  const [metrics, setMetrics] = React.useState<Metrics | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const refresh = React.useCallback(async () => {
    try {
      const [q, s, m] = await Promise.all([
        api.escalations(),
        api.shadow(),
        api.metrics(),
      ]);
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

  const decide = async (item: Escalation, answer: "approve" | "deny") => {
    setBusy(item.id);
    try {
      await api.decide(item.id, answer);
      await refresh();
      setError(null);
    } catch (cause) {
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
      <Box
        display="flex"
        gap="spacing.4"
        alignItems="center"
        paddingY="spacing.10"
      >
        <Spinner accessibilityLabel="Loading operations" />
        <Text>Reading the checkpoint…</Text>
      </Box>
    );
  }

  return (
    <Box>
      <PageHeader
        eyebrow="Operations"
        title="Who is waiting, and what would have been stopped."
        lede="The approval queue, the shadow report, and the numbers worth an alarm."
      />

      <Box
        marginTop="spacing.6"
        display="flex"
        flexDirection="column"
        gap="spacing.4"
      >
        {error ? (
          <Alert color="negative" description={error} isDismissible={false} />
        ) : null}
        {!metrics.chain_intact ? (
          <Alert
            color="negative"
            title="The audit chain does not verify"
            description="This is corruption, or somebody editing history. Both are incidents."
            isDismissible={false}
          />
        ) : null}
        {shadow.mode === "observe" ? (
          <Alert
            color="notice"
            title="Observing — nothing is being stopped"
            description="Every check runs and every decision is recorded. Refusals are not acted on."
            isDismissible={false}
          />
        ) : null}
      </Box>

      {/* ------------------------------------------------------------ approvals */}
      <Section
        title="Waiting for a person"
        caption={`An approval releases exactly one request, exactly once, and only until the window closes. Nobody answers and it resolves to ${queue.on_timeout} after ${queue.timeout_seconds}s.`}
      >
        <Box display="flex" gap="spacing.3" marginBottom="spacing.5">
          <Badge
            color={queue.pending.length ? "notice" : "neutral"}
            emphasis="subtle"
            size="large"
          >
            {`${queue.pending.length} pending`}
          </Badge>
          <Badge color="neutral" emphasis="subtle" size="large">
            {`${queue.approvers_configured} approver${queue.approvers_configured === 1 ? "" : "s"}`}
          </Badge>
        </Box>

        {queue.approvers_configured === 0 ? (
          <Box marginBottom="spacing.5">
            <Alert
              color="notice"
              description="No approver credential is configured, so every step-up will time out. That is the fail-closed direction."
              isDismissible={false}
            />
          </Box>
        ) : null}

        {queue.pending.length === 0 ? (
          <Card elevation="lowRaised" padding="spacing.7">
            <CardBody>
              <Text color="surface.text.gray.muted">
                Nothing is waiting. A step-up appears here when an action goes
                above the merchant&rsquo;s auto-approval band.
              </Text>
            </CardBody>
          </Card>
        ) : (
          <Card elevation="lowRaised" padding="spacing.0">
            <CardBody>
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
                          </TableCell>
                          <TableCell>
                            <Amount
                              value={item.amount / 100}
                              size="large"
                              weight="semibold"
                            />
                          </TableCell>
                          <TableCell>
                            <Text size="small" color="surface.text.gray.muted">
                              {item.session_id}
                            </Text>
                          </TableCell>
                          <TableCell>
                            <Box display="flex" gap="spacing.3">
                              <Button
                                size="small"
                                variant="primary"
                                isLoading={busy === item.id}
                                onClick={() => void decide(item, "approve")}
                              >
                                Approve
                              </Button>
                              <Button
                                size="small"
                                variant="secondary"
                                color="negative"
                                isDisabled={busy === item.id}
                                onClick={() => void decide(item, "deny")}
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
            </CardBody>
          </Card>
        )}

        {queue.expired.length > 0 ? (
          <Box marginTop="spacing.4">
            <Text size="small" color="surface.text.gray.muted">
              {`${queue.expired.length} ran out of time and resolved to ${queue.on_timeout}. Shown rather than deleted — "nobody answered" is a number worth watching.`}
            </Text>
          </Box>
        ) : null}
      </Section>

      {/* ------------------------------------------------------------ shadow */}
      <Section
        title="What enforcement would have changed"
        caption="Counted from the audit chain, not a running tally. Every number is zero while the mode is enforce — and that is the right answer to “what did you let through”."
      >
        <Box display="flex" gap="spacing.5" flexWrap="wrap">
          <Stat
            label="Mode"
            value={shadow.mode}
            tone={shadow.enforcing ? "positive" : "notice"}
          />
          <Stat label="Decisions" value={String(shadow.decisions)} />
          <Stat
            label="Would have been stopped"
            value={String(shadow.observed)}
            tone={shadow.observed ? "notice" : undefined}
            hint={`${(shadow.rate * 100).toFixed(2)}% of decisions`}
          />
          <Stat
            label="Money at risk"
            amount={shadow.money_at_risk}
            tone={shadow.money_at_risk ? "notice" : undefined}
            hint="not saved — would not have moved"
          />
        </Box>
      </Section>

      {/* ------------------------------------------------------------ metrics */}
      <Section title="What has happened">
        <Box display="flex" gap="spacing.5" flexWrap="wrap">
          <Stat
            label="Chain intact"
            value={metrics.chain_intact ? "yes" : "NO"}
            tone={metrics.chain_intact ? "positive" : "negative"}
            hint={`${metrics.chain_records} records`}
          />
          <Stat label="Decisions" value={String(metrics.decisions)} />
          <Stat
            label="Allowed"
            value={String(metrics.allowed)}
            tone="positive"
          />
          <Stat label="Denied" value={String(metrics.denied)} tone="negative" />
          <Stat
            label="Sent to a human"
            value={String(metrics.stepped_up)}
            tone="notice"
          />
          <Stat
            label="Replayed"
            value={String(metrics.replayed)}
            hint="duplicates, not charges"
          />
          <Stat label="Money moved" amount={metrics.money_moved} />
          <Stat
            label="Breaker trips"
            value={String(metrics.breaker_trips)}
            tone={metrics.breaker_trips ? "notice" : undefined}
          />
        </Box>
      </Section>
    </Box>
  );
}
