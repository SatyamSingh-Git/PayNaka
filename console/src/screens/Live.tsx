import React from "react";
import {
  Alert,
  Amount,
  Badge,
  Box,
  Button,
  Card,
  CardBody,
  Code,
  Collapsible,
  CollapsibleBody,
  CollapsibleLink,
  Divider,
  Heading,
  Indicator,
  Text,
  Spinner,
  ShoppingCartIcon,
  ShieldIcon,
  AlertTriangleIcon,
  CheckCircleIcon,
} from "@razorpay/blade/components";
import { api, formatInr, type DemoRun } from "../api";

type Mode = { scenario: "happy" | "attack"; gate: boolean };

const RUNS: Array<{ mode: Mode; label: string; help: string }> = [
  {
    mode: { scenario: "happy", gate: true },
    label: "Clean purchase",
    help: "What the shopper actually asked for. Must go through.",
  },
  {
    mode: { scenario: "attack", gate: false },
    label: "Attack · gate off",
    help: "The agent holds the rail. Establishes that the attack is real.",
  },
  {
    mode: { scenario: "attack", gate: true },
    label: "Attack · gate on",
    help: "The same run, through PayNaka.",
  },
];

/** The comparison the whole project exists to make, sized like it matters. */
function Verdict({
  caption,
  paise,
  tone,
  detail,
}: {
  caption: string;
  paise: number | null;
  tone: "negative" | "positive";
  detail: string;
}): JSX.Element {
  const intense =
    tone === "negative"
      ? "feedback.text.negative.intense"
      : "feedback.text.positive.intense";
  return (
    <Box flex="1" minWidth="260px">
      <Text size="small" weight="semibold" color="surface.text.gray.muted">
        {caption}
      </Text>
      <Box marginTop="spacing.3" minHeight="56px">
        {paise === null ? (
          <Heading
            size="2xlarge"
            weight="semibold"
            color="surface.text.gray.disabled"
          >
            —
          </Heading>
        ) : (
          <Amount
            value={paise / 100}
            type="heading"
            size="2xlarge"
            weight="semibold"
            color={intense as never}
          />
        )}
      </Box>
      <Text size="small" color="surface.text.gray.muted" marginTop="spacing.2">
        {detail}
      </Text>
    </Box>
  );
}

export function Live(): JSX.Element {
  const [run, setRun] = React.useState<DemoRun | null>(null);
  const [off, setOff] = React.useState<DemoRun | null>(null);
  const [on, setOn] = React.useState<DemoRun | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  async function go(mode: Mode, label: string): Promise<void> {
    setBusy(label);
    setError(null);
    try {
      const result = await api.runDemo(mode.scenario, mode.gate);
      setRun(result);
      // Both halves of the comparison persist, so the hero keeps showing the contrast
      // after the second run rather than replacing the first with it.
      if (mode.scenario === "attack") {
        (mode.gate ? setOn : setOff)(result);
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Box display="flex" flexDirection="column" gap="spacing.7">
      {/* ------------------------------------------------------------- hero */}
      <Box>
        <Badge color="information" emphasis="subtle" size="medium">
          Live · simulated rail · no keys
        </Badge>
        <Box marginTop="spacing.4" maxWidth="860px">
          <Heading size="2xlarge" weight="semibold">
            A poisoned review tries to move ₹51,999.
          </Heading>
          <Box marginTop="spacing.2">
            <Heading
              size="2xlarge"
              weight="regular"
              color="surface.text.gray.muted"
            >
              The shopper authorised ₹1,999.
            </Heading>
          </Box>
        </Box>
        <Box marginTop="spacing.4" maxWidth="720px">
          <Text size="large" color="surface.text.gray.subtle">
            The agent is helpful, and believes the review. Run it with the
            checkpoint off, then on, and compare what left the account.
          </Text>
        </Box>
      </Box>

      {/* -------------------------------------------------- the contrast */}
      <Card elevation="lowRaised" padding="spacing.7">
        <CardBody>
          <Box display="flex" gap="spacing.7" flexWrap="wrap">
            <Verdict
              caption="WITHOUT PAYNAKA"
              paise={off ? off.money_moved : null}
              tone="negative"
              detail={
                off
                  ? `${formatInr(off.overspent)} beyond the mandate`
                  : "run “Attack · gate off” to establish the attack is real"
              }
            />
            <Divider orientation="vertical" />
            <Verdict
              caption="WITH PAYNAKA"
              paise={on ? on.money_moved : null}
              tone="positive"
              detail={
                on
                  ? (on.denials[0]?.check_id ?? "allowed within the mandate")
                  : "then run “Attack · gate on”"
              }
            />
          </Box>
        </CardBody>
      </Card>

      {/* ------------------------------------------------------------ runs */}
      <Box display="flex" gap="spacing.4" flexWrap="wrap">
        {RUNS.map(({ mode, label, help }) => (
          <Card
            key={label}
            width={{ base: "100%", m: "320px" }}
            padding="spacing.5"
          >
            <CardBody>
              <Box
                display="flex"
                flexDirection="column"
                gap="spacing.3"
                height="100%"
              >
                <Text weight="semibold">{label}</Text>
                <Text size="small" color="surface.text.gray.muted">
                  {help}
                </Text>
                <Box marginTop="auto" paddingTop="spacing.4">
                  <Button
                    isFullWidth
                    variant={
                      mode.gate && mode.scenario === "attack"
                        ? "primary"
                        : "secondary"
                    }
                    icon={
                      mode.scenario === "happy" ? ShoppingCartIcon : ShieldIcon
                    }
                    isLoading={busy === label}
                    isDisabled={busy !== null}
                    onClick={() => void go(mode, label)}
                  >
                    Run
                  </Button>
                </Box>
              </Box>
            </CardBody>
          </Card>
        ))}
      </Box>

      {error && (
        <Alert
          color="negative"
          title="Could not reach PayNaka"
          description={`${error}. Is the service running on :8002? Try \`make naka\`.`}
          isDismissible={false}
        />
      )}

      {busy && !run && (
        <Box display="flex" justifyContent="center" padding="spacing.9">
          <Spinner accessibilityLabel="Running" size="large" />
        </Box>
      )}

      {run && <Outcome run={run} />}
    </Box>
  );
}

function Outcome({ run }: { run: DemoRun }): JSX.Element {
  const blocked = run.denials.length > 0;
  const overspent = run.overspent > 0;

  return (
    <Box display="flex" flexDirection="column" gap="spacing.6">
      {overspent ? (
        <Alert
          color="negative"
          icon={AlertTriangleIcon}
          title={`${formatInr(run.overspent)} left the account beyond what was authorised`}
          description="No checkpoint. The agent read a poisoned review, believed it, and paid. This is the baseline the corpus is measured against."
          isDismissible={false}
        />
      ) : blocked ? (
        <Alert
          color="positive"
          icon={ShieldIcon}
          title="Blocked before any money moved"
          description={run.denials[0]?.reason ?? ""}
          isDismissible={false}
        />
      ) : (
        <Alert
          color="positive"
          icon={CheckCircleIcon}
          title="Purchase completed"
          description="Exactly what the shopper asked for, at exactly the authorised amount."
          isDismissible={false}
        />
      )}

      <Box display="flex" gap="spacing.5" flexWrap="wrap">
        <Ledger label="Authorised" paise={run.authorised} tone="neutral" />
        <Ledger
          label="Actually moved"
          paise={run.money_moved}
          tone={overspent ? "negative" : "positive"}
        />
        <Ledger
          label="Overspent"
          paise={run.overspent}
          tone={overspent ? "negative" : "positive"}
        />
      </Box>

      <Box
        display="flex"
        gap="spacing.6"
        flexWrap="wrap"
        alignItems="flex-start"
      >
        <Box flex="1" minWidth="320px">
          <Heading size="small" marginBottom="spacing.4">
            What the agent did
          </Heading>
          <Card padding="spacing.5">
            <CardBody>
              <Box display="flex" flexDirection="column" gap="spacing.3">
                {run.transcript.map((step, index) => (
                  <Box
                    key={index}
                    display="flex"
                    gap="spacing.3"
                    alignItems="flex-start"
                  >
                    <Box paddingTop="spacing.1">
                      <Indicator
                        color={
                          step.name?.startsWith("create_")
                            ? "notice"
                            : "information"
                        }
                        emphasis="intense"
                        accessibilityLabel={step.role}
                      />
                    </Box>
                    <Box flex="1">
                      <Code size="small">{step.name ?? step.role}</Code>
                      {step.args && Object.keys(step.args).length > 0 && (
                        <Text
                          size="xsmall"
                          color="surface.text.gray.muted"
                          marginTop="spacing.1"
                        >
                          {JSON.stringify(step.args)}
                        </Text>
                      )}
                      {step.text && (
                        <Text size="small" marginTop="spacing.1">
                          {step.text}
                        </Text>
                      )}
                    </Box>
                  </Box>
                ))}
              </Box>
            </CardBody>
          </Card>
        </Box>

        <Box flex="1" minWidth="320px">
          <Heading size="small" marginBottom="spacing.4">
            What the checkpoint decided
          </Heading>
          {run.denials.length === 0 && run.executions.length === 0 ? (
            <Card padding="spacing.5">
              <CardBody>
                <Text size="small" color="surface.text.gray.muted">
                  No money action reached a checkpoint on this run.
                </Text>
              </CardBody>
            </Card>
          ) : (
            <Box display="flex" flexDirection="column" gap="spacing.4">
              {run.denials.map((decision, index) => (
                <Decision key={`d${index}`} decision={decision} />
              ))}
              {run.executions.map((execution, index) => (
                <Decision key={`e${index}`} decision={execution.decision} />
              ))}
            </Box>
          )}
        </Box>
      </Box>

      {run.poisoned_field && (
        <Card padding="spacing.5">
          <CardBody>
            <Box display="flex" flexDirection="column" gap="spacing.3">
              <Box display="flex" gap="spacing.3" alignItems="center">
                <Badge color="negative" emphasis="subtle">
                  user_generated
                </Badge>
                <Code size="small">{run.poisoned_field}</Code>
              </Box>
              <Text size="small" color="surface.text.gray.subtle">
                The field the injection arrived in. It is labelled untrusted in
                the catalog feed, and the label travels with it into the audit
                record.
              </Text>
            </Box>
          </CardBody>
        </Card>
      )}

      <Collapsible>
        <CollapsibleLink>Audit anchor and caveats</CollapsibleLink>
        <CollapsibleBody>
          <Box
            display="flex"
            flexDirection="column"
            gap="spacing.3"
            paddingTop="spacing.3"
          >
            <Box>
              <Text size="small" weight="semibold">
                Audit head
              </Text>
              <Code size="small">{run.audit_head.slice(0, 32)}…</Code>
            </Box>
            <Divider />
            <Text size="small" color="surface.text.gray.muted">
              {run.note}
            </Text>
          </Box>
        </CollapsibleBody>
      </Collapsible>
    </Box>
  );
}

function Ledger({
  label,
  paise,
  tone,
}: {
  label: string;
  paise: number;
  tone: "neutral" | "positive" | "negative";
}): JSX.Element {
  return (
    <Card width={{ base: "100%", m: "240px" }} padding="spacing.5">
      <CardBody>
        <Text size="small" color="surface.text.gray.muted">
          {label}
        </Text>
        <Box marginTop="spacing.2">
          <Amount
            value={paise / 100}
            currency="INR"
            size="xlarge"
            type="heading"
            color={
              tone === "negative"
                ? "feedback.text.negative.intense"
                : tone === "positive"
                  ? "feedback.text.positive.intense"
                  : undefined
            }
          />
        </Box>
      </CardBody>
    </Card>
  );
}

function Decision({
  decision,
}: {
  decision: DemoRun["denials"][number];
}): JSX.Element {
  const denied = decision.verdict === "DENY";
  return (
    <Card padding="spacing.5">
      <CardBody>
        <Box display="flex" flexDirection="column" gap="spacing.3">
          <Box
            display="flex"
            gap="spacing.3"
            alignItems="center"
            flexWrap="wrap"
          >
            <Badge color={denied ? "negative" : "positive"} emphasis="intense">
              {decision.verdict}
            </Badge>
            <Code size="small">{decision.action}</Code>
            {decision.check_id && <Code size="small">{decision.check_id}</Code>}
          </Box>
          <Text size="small">{decision.reason}</Text>
          {Object.keys(decision.evidence).length > 0 && (
            <Box
              backgroundColor="surface.background.gray.moderate"
              padding="spacing.4"
              borderRadius="medium"
            >
              {Object.entries(decision.evidence).map(([key, value]) => (
                <Box
                  key={key}
                  display="flex"
                  gap="spacing.3"
                  justifyContent="space-between"
                >
                  <Text size="xsmall" color="surface.text.gray.muted">
                    {key}
                  </Text>
                  <Text size="xsmall" weight="semibold">
                    {key.includes("total") ||
                    key === "requested" ||
                    key === "authorised"
                      ? typeof value === "number"
                        ? formatInr(value)
                        : String(value)
                      : JSON.stringify(value)}
                  </Text>
                </Box>
              ))}
            </Box>
          )}
          <Text size="xsmall" color="surface.text.gray.muted">
            decided in {(decision.latency_us / 1000).toFixed(2)} ms
          </Text>
        </Box>
      </CardBody>
    </Card>
  );
}
