import React from 'react';
import {
  Alert,
  Badge,
  BarChartIcon,
  Box,
  Card,
  CardBody,
  EmptyState,
  Heading,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableHeaderRow,
  TableRow,
  Text,
} from '@razorpay/blade/components';

interface DefenceRow {
  id: string;
  defence: string;
  attacks: number;
  attack_success_rate: number;
  benign: number;
  benign_pass_rate: number;
  total_overspent_paise: number;
  median_latency_ms: number;
  p95_latency_ms: number;
  refusals: number;
}

interface Results {
  corpus: string;
  generated: string;
  runs: number;
  defences: Omit<DefenceRow, 'id'>[];
}

const LABELS: Record<string, string> = {
  none: 'None — agent holds the rail',
  prompt: 'Prompt hardening',
  judge: 'LLM judge (FinHarness-style)',
  naka: 'PayNaka',
};

export function Benchmark(): JSX.Element {
  const [results, setResults] = React.useState<Results | null>(null);
  const [missing, setMissing] = React.useState(false);

  React.useEffect(() => {
    fetch('/RESULTS.json')
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error('absent'))))
      .then(setResults)
      .catch(() => setMissing(true));
  }, []);

  const rows: DefenceRow[] = (results?.defences ?? []).map((row, index) => ({
    ...row,
    id: String(index),
  }));

  return (
    <Box display="flex" flexDirection="column" gap="spacing.6">
      <Box>
        <Heading size="large">HAAT</Heading>
        <Text color="surface.text.gray.subtle" marginTop="spacing.2">
          Four defences, one corpus. An attack succeeds when money moves beyond what the
          mandate authorised — not when a gate returns DENY.
        </Text>
      </Box>

      {missing && !results && (
        <EmptyState
          asset={<BarChartIcon size="2xlarge" color="surface.icon.gray.muted" />}
          title="No results yet"
          description="Run `make bench` to generate RESULTS.json. It needs an Anthropic key, because HAAT measures whether a real model can be talked into moving money, and only a real model can answer that."
          size="large"
        />
      )}

      {results && (
        <>
          <Alert
            color="information"
            title={`${results.runs} runs over the ${results.corpus} corpus`}
            description={`Generated ${results.generated}. Every number comes from the committed runner over the committed corpus; none is typed by hand.`}
            isDismissible={false}
          />

          <Card padding="spacing.5">
            <CardBody>
              <Table data={{ nodes: rows }}>
                {(tableData) => (
                  <>
                    <TableHeader>
                      <TableHeaderRow>
                        <TableHeaderCell>Defence</TableHeaderCell>
                        <TableHeaderCell>Attack success</TableHeaderCell>
                        <TableHeaderCell>Benign pass</TableHeaderCell>
                        <TableHeaderCell>Median</TableHeaderCell>
                        <TableHeaderCell>p95</TableHeaderCell>
                        <TableHeaderCell>Refusals</TableHeaderCell>
                      </TableHeaderRow>
                    </TableHeader>
                    <TableBody>
                      {tableData.map((row) => (
                        <TableRow key={row.id} item={row}>
                          <TableCell>
                            <Text weight={row.defence === 'naka' ? 'semibold' : 'regular'}>
                              {LABELS[row.defence] ?? row.defence}
                            </Text>
                          </TableCell>
                          <TableCell>
                            <Badge
                              color={row.attack_success_rate > 0.05 ? 'negative' : 'positive'}
                              emphasis="subtle"
                            >
                              {(row.attack_success_rate * 100).toFixed(1)}%
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <Badge
                              color={row.benign_pass_rate < 0.95 ? 'notice' : 'positive'}
                              emphasis="subtle"
                            >
                              {(row.benign_pass_rate * 100).toFixed(1)}%
                            </Badge>
                          </TableCell>
                          <TableCell>{`${row.median_latency_ms.toFixed(0)} ms`}</TableCell>
                          <TableCell>{`${row.p95_latency_ms.toFixed(0)} ms`}</TableCell>
                          <TableCell>{String(row.refusals)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </>
                )}
              </Table>
            </CardBody>
          </Card>

          <Card padding="spacing.5">
            <CardBody>
              <Text size="small" color="surface.text.gray.subtle">
                Read both columns together or not at all. A defence that refuses everything
                scores 0% attack success and 0% benign pass — that is not a defence, it is an
                outage. A model refusal is neither an attack success nor a defensive win, so
                it is counted on its own rather than folded into either.
              </Text>
            </CardBody>
          </Card>
        </>
      )}
    </Box>
  );
}
