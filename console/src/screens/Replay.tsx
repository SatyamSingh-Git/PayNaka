import React from 'react';
import {
  Alert,
  AlertTriangleIcon,
  Badge,
  Box,
  Button,
  Card,
  CardBody,
  CheckCircleIcon,
  Code,
  Divider,
  EmptyState,
  Heading,
  HistoryIcon,
  Text,
} from '@razorpay/blade/components';
import { api, formatInr, type AuditRecord } from '../api';

export function Replay(): JSX.Element {
  const [records, setRecords] = React.useState<AuditRecord[]>([]);
  const [head, setHead] = React.useState('');
  const [verified, setVerified] = React.useState<{ intact: boolean; records: number } | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(() => {
    api
      .audit()
      .then((data) => {
        setRecords(data.records);
        setHead(data.head);
      })
      .catch((exc) => setError(String(exc)));
  }, []);

  React.useEffect(() => {
    load();
  }, [load]);

  return (
    <Box display="flex" flexDirection="column" gap="spacing.6">
      <Box
        display="flex"
        justifyContent="space-between"
        alignItems="flex-start"
        flexWrap="wrap"
        gap="spacing.4"
      >
        <Box>
          <Heading size="large">Audit chain</Heading>
          <Text color="surface.text.gray.subtle" marginTop="spacing.2">
            Append-only and hash-linked. Every decision, allowed or refused.
          </Text>
        </Box>
        <Box display="flex" gap="spacing.3">
          <Button variant="secondary" onClick={load}>
            Refresh
          </Button>
          <Button
            icon={CheckCircleIcon}
            onClick={() => {
              api.verifyAudit().then(setVerified).catch((exc) => setError(String(exc)));
            }}
          >
            Verify chain
          </Button>
        </Box>
      </Box>

      {error && (
        <Alert
          color="negative"
          title="Could not reach PayNaka"
          description={error}
          isDismissible={false}
        />
      )}

      {verified && (
        <Alert
          color={verified.intact ? 'positive' : 'negative'}
          icon={verified.intact ? CheckCircleIcon : AlertTriangleIcon}
          title={
            verified.intact
              ? `Chain intact across ${verified.records} records`
              : 'Chain broken'
          }
          description={
            verified.intact
              ? 'Every record was rehashed against its predecessor. Editing or removing one would break every hash after it, and verification would name the exact record.'
              : 'A record has been edited or removed. The API response carries the sequence number and the kind of break.'
          }
          isDismissible={false}
        />
      )}

      {head && (
        <Card padding="spacing.5">
          <CardBody>
            <Text size="small" color="surface.text.gray.muted">
              Head
            </Text>
            <Code size="small">{head}</Code>
            <Text size="xsmall" color="surface.text.gray.muted" marginTop="spacing.2">
              Publish this somewhere outside our control. The chain proves internal
              consistency, not authenticity — only an external copy of the head catches a
              wholesale rewrite.
            </Text>
          </CardBody>
        </Card>
      )}

      {records.length === 0 ? (
        <EmptyState
          asset={<HistoryIcon size="2xlarge" color="surface.icon.gray.muted" />}
          title="Nothing recorded yet"
          description="Run a scenario on the Live screen. Every decision lands here, including the refused ones."
          size="large"
        />
      ) : (
        <Box display="flex" flexDirection="column" gap="spacing.3">
          {records.map((record) => (
            <Record key={record.seq} record={record} />
          ))}
        </Box>
      )}
    </Box>
  );
}

function Record({ record }: { record: AuditRecord }): JSX.Element {
  const payload = record.payload;
  const decision = payload.decision;
  const denied = decision?.verdict === 'DENY';
  const reads: Array<{ sku: string; review_count: number; untrusted_fields: string[] }> =
    payload.provenance?.reads ?? [];

  return (
    <Card padding="spacing.5">
      <CardBody>
        <Box display="flex" flexDirection="column" gap="spacing.3">
          <Box display="flex" gap="spacing.3" alignItems="center" flexWrap="wrap">
            <Badge emphasis="subtle">{`#${record.seq}`}</Badge>
            <Code size="small">{payload.kind}</Code>
            {decision && (
              <Badge color={denied ? 'negative' : 'positive'} emphasis="intense">
                {decision.verdict}
              </Badge>
            )}
            {decision?.check_id && <Code size="small">{decision.check_id}</Code>}
          </Box>

          {decision?.reason && <Text size="small">{decision.reason}</Text>}

          {payload.request && (
            <Box display="flex" gap="spacing.5" flexWrap="wrap">
              <Text size="xsmall" color="surface.text.gray.muted">
                {`${payload.request.action} · ${formatInr(payload.request.amount ?? 0)}`}
              </Text>
              {payload.mandate && (
                <Text size="xsmall" color="surface.text.gray.muted">
                  {`authorised ${formatInr(payload.mandate.max_total)}`}
                </Text>
              )}
            </Box>
          )}

          {reads.length > 0 && (
            <>
              <Divider />
              <Box>
                <Text size="xsmall" color="surface.text.gray.muted">
                  What the agent had read before this decision
                </Text>
                {reads.map((read, index) => (
                  <Box
                    key={index}
                    display="flex"
                    gap="spacing.3"
                    marginTop="spacing.2"
                    flexWrap="wrap"
                    alignItems="center"
                  >
                    <Code size="small">{read.sku}</Code>
                    <Text size="xsmall" color="surface.text.gray.muted">
                      {`${read.review_count} review(s)`}
                    </Text>
                    {read.untrusted_fields?.length > 0 && (
                      <Badge color="notice" emphasis="subtle">
                        {`untrusted: ${read.untrusted_fields.join(', ')}`}
                      </Badge>
                    )}
                  </Box>
                ))}
              </Box>
            </>
          )}

          <Box display="flex" gap="spacing.4" flexWrap="wrap">
            <Text size="xsmall" color="surface.text.gray.muted">
              {`hash ${record.hash.slice(0, 16)}…`}
            </Text>
            <Text size="xsmall" color="surface.text.gray.muted">
              {`prev ${record.prev_hash.slice(0, 16)}…`}
            </Text>
          </Box>
        </Box>
      </CardBody>
    </Card>
  );
}
