import React from 'react';
import {
  Alert,
  Badge,
  Box,
  Card,
  CardBody,
  Code,
  Divider,
  Heading,
  InfoGroup,
  InfoItem,
  InfoItemKey,
  InfoItemValue,
  LockIcon,
  Spinner,
  Text,
} from '@razorpay/blade/components';
import { api, type PolicyView } from '../api';

const ACTION_LABELS: Record<string, string> = {
  create_order: 'Create order',
  capture_payment: 'Capture payment',
  create_payment_link: 'Create payment link',
  create_refund: 'Create refund',
  create_payout: 'Create payout',
};

const REGULATION: Array<{ key: keyof PolicyView['regulatory']; label: string; source: string }> = [
  { key: 'npci_mandate_retries', label: 'Mandate retries per cycle', source: 'NPCI' },
  { key: 'debit_blackout', label: 'Recurring-debit blackout', source: 'NPCI' },
  { key: 'contact_window', label: 'Customer contact window', source: 'RBI' },
  { key: 'afa_threshold_formatted', label: 'Additional-factor threshold', source: 'RBI' },
  { key: 'pre_debit_notice_seconds', label: 'Pre-debit notice', source: 'RBI' },
];

export function Policy(): JSX.Element {
  const [policy, setPolicy] = React.useState<PolicyView | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    api.policy().then(setPolicy).catch((exc) => setError(String(exc)));
  }, []);

  if (error) {
    return (
      <Alert
        color="negative"
        title="Could not reach PayNaka"
        description={error}
        isDismissible={false}
      />
    );
  }

  if (!policy) {
    return (
      <Box display="flex" justifyContent="center" padding="spacing.9">
        <Spinner accessibilityLabel="Loading policy" size="large" />
      </Box>
    );
  }

  return (
    <Box display="flex" flexDirection="column" gap="spacing.6">
      <Box>
        <Heading size="large">Policy</Heading>
        <Text color="surface.text.gray.subtle" marginTop="spacing.2">
          The merchant&rsquo;s envelope. It narrows what a mandate already permits and can
          never widen it — the effective permission is always the intersection.
        </Text>
      </Box>

      <Alert
        color="information"
        icon={LockIcon}
        title="An unanswered approval fails closed"
        description={`Step-up requests time out after ${policy.step_up_timeout_seconds}s and resolve to ${policy.on_step_up_timeout}. That is not configurable — making it a knob invites someone to turn it the wrong way at 3am during an incident.`}
        isDismissible={false}
      />

      <Box>
        <Heading size="small" marginBottom="spacing.4">
          Per action
        </Heading>
        <Box display="flex" gap="spacing.4" flexWrap="wrap">
          {Object.entries(policy.actions).map(([name, config]) => (
            <Card key={name} width={{ base: '100%', m: '320px' }} padding="spacing.5">
              <CardBody>
                <Box display="flex" flexDirection="column" gap="spacing.3">
                  <Box display="flex" justifyContent="space-between" alignItems="center" gap="spacing.3">
                    <Text weight="semibold">{ACTION_LABELS[name] ?? name}</Text>
                    <Badge color={config.enabled ? 'positive' : 'negative'} emphasis="subtle">
                      {config.enabled ? 'enabled' : 'disabled'}
                    </Badge>
                  </Box>

                  {!config.enabled ? (
                    <Text size="small" color="surface.text.gray.muted">
                      Not something a shopping agent has any business initiating.
                    </Text>
                  ) : (
                    <InfoGroup itemOrientation="horizontal" size="small">
                      {config.max_amount_formatted && (
                        <InfoItem>
                          <InfoItemKey>Ceiling</InfoItemKey>
                          <InfoItemValue>{config.max_amount_formatted}</InfoItemValue>
                        </InfoItem>
                      )}
                      {config.step_up_above_formatted && (
                        <InfoItem>
                          <InfoItemKey>Human approval above</InfoItemKey>
                          <InfoItemValue>{config.step_up_above_formatted}</InfoItemValue>
                        </InfoItem>
                      )}
                      {config.daily_cap_formatted && (
                        <InfoItem>
                          <InfoItemKey>Daily cap (IST)</InfoItemKey>
                          <InfoItemValue>{config.daily_cap_formatted}</InfoItemValue>
                        </InfoItem>
                      )}
                      {config.require_return_event && (
                        <InfoItem>
                          <InfoItemKey>Requires</InfoItemKey>
                          <InfoItemValue>a return on record</InfoItemValue>
                        </InfoItem>
                      )}
                    </InfoGroup>
                  )}
                </Box>
              </CardBody>
            </Card>
          ))}
        </Box>
      </Box>

      <Divider />

      <Box>
        <Heading size="small" marginBottom="spacing.2">
          Indian payments regulation
        </Heading>
        <Text size="small" color="surface.text.gray.subtle" marginBottom="spacing.4">
          Enforced, not documented. Each of these is a deterministic check in the gate,
          keyed to an injected clock so an 08:00 rule is testable at any hour.
        </Text>

        <Card padding="spacing.5">
          <CardBody>
            <Box display="flex" flexDirection="column" gap="spacing.4">
              {REGULATION.map(({ key, label, source }) => {
                const raw = policy.regulatory[key];
                const value = Array.isArray(raw)
                  ? raw.join(', ') || 'none'
                  : key === 'pre_debit_notice_seconds'
                    ? `${Math.round(Number(raw) / 3600)}h`
                    : String(raw ?? '—');
                return (
                  <Box
                    key={String(key)}
                    display="flex"
                    justifyContent="space-between"
                    alignItems="center"
                    gap="spacing.4"
                    flexWrap="wrap"
                  >
                    <Box display="flex" gap="spacing.3" alignItems="center">
                      <Badge emphasis="subtle">{source}</Badge>
                      <Text size="small">{label}</Text>
                    </Box>
                    <Code size="small">{value}</Code>
                  </Box>
                );
              })}
            </Box>
          </CardBody>
        </Card>
      </Box>

      <Card padding="spacing.5">
        <CardBody>
          <Text size="small" color="surface.text.gray.subtle">
            An action with no entry in this policy is <b>disabled</b>, not unrestricted. A
            policy that forgot to mention payouts must not thereby permit unlimited
            payouts, and a typo in a key name is a startup failure rather than a silently
            removed limit.
          </Text>
        </CardBody>
      </Card>
    </Box>
  );
}
