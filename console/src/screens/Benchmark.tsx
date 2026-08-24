import React from "react";
import {
  Alert,
  Badge,
  BarChartIcon,
  Box,
  Card,
  CardBody,
  Divider,
  EmptyState,
  Heading,
  ProgressBar,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableHeaderRow,
  TableRow,
  Text,
} from "@razorpay/blade/components";
import { PageHeader } from "../ui";

/**
 * What this project has actually measured.
 *
 * This screen used to fetch a single `/RESULTS.json` that nothing produced, from a
 * directory that did not exist, and render its empty state on every run since it was
 * written. It now shows four things separately, because they are separate claims with
 * very different evidence behind them, and stacking them into one number would be the
 * easiest way to overstate the project.
 *
 *   TOCTOU     deterministic. The attack no prompt and no smarter model can defend.
 *   Chaos      deterministic, no model, no keys. Runs anywhere. Always present.
 *   Sentinel   a measured detector, reported with its false-positive rate and its margin.
 *   HAAT       needs a model key and real spend. Absent until `make bench` has run.
 *
 * `make console-data` writes the first three into console/public.
 */

// ------------------------------------------------------------------ chaos

interface ChaosSide {
  handler: string;
  left_the_gateway: number;
  ledger_says: number;
  entitled: number;
  overspent: number;
  underpaid: number;
  books_disagree: number;
  named_refusals: string[];
  silent_drops: number;
}

interface ChaosScenario {
  key: string;
  title: string;
  hazard: string;
  why: string;
  naive: ChaosSide;
  paynaka: ChaosSide;
  prevented: number;
}

interface ChaosResults {
  scenarios: ChaosScenario[];
  totals: {
    naive_overspent: number;
    paynaka_overspent: number;
    naive_underpaid: number;
  };
}

// ------------------------------------------------------------------ toctou

interface TocTouRun {
  defence: string;
  moment: string;
  mutation: string;
  listed: number;
  charged_price: number;
  authorised: number;
  money_moved: number;
  overspent: number;
  overpaid_vs_listed: number;
  check_id: string | null;
}

interface TocTouResults {
  listed: number;
  authorised: number;
  mutations: { key: string; label: string; why: string; charged: number }[];
  runs: TocTouRun[];
  totals: Record<string, number>;
}

// ------------------------------------------------------------------ sentinel

interface SentinelResults {
  threshold: number;
  attacks: number;
  negatives: number;
  caught: number;
  missed: number;
  false_positives: number;
  recall: number;
  precision: number;
  false_positive_rate: number;
  margin: number;
  nearest_miss?: {
    case_id: string;
    score: number;
    threshold: number;
    rules: string[];
    text: string;
  };
}

// ------------------------------------------------------------------ haat

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

interface BenchResults {
  corpus: string;
  generated: string;
  runs: number;
  defences: Omit<DefenceRow, "id">[];
}

const LABELS: Record<string, string> = {
  none: "None — agent holds the rail",
  prompt: "Prompt hardening",
  judge: "LLM judge (FinHarness-style)",
  naka: "PayNaka",
};

/** Integer paise in, Indian-grouped rupees out. Formatting happens once, at the edge. */
function inr(paise: number): string {
  const sign = paise < 0 ? "-" : "";
  const whole = Math.floor(Math.abs(paise) / 100);
  const frac = String(Math.abs(paise) % 100).padStart(2, "0");
  const digits = String(whole);
  const grouped =
    digits.length > 3
      ? digits.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",") +
        "," +
        digits.slice(-3)
      : digits;
  return `${sign}₹${grouped}.${frac}`;
}

function useJson<T>(path: string): { data: T | null; missing: boolean } {
  const [data, setData] = React.useState<T | null>(null);
  const [missing, setMissing] = React.useState(false);
  React.useEffect(() => {
    fetch(path)
      .then((response) =>
        response.ok ? response.json() : Promise.reject(new Error("absent")),
      )
      .then(setData)
      .catch(() => setMissing(true));
  }, [path]);
  return { data, missing };
}

export function Benchmark(): JSX.Element {
  const toctou = useJson<TocTouResults>("/toctou.json");
  const chaos = useJson<ChaosResults>("/chaos.json");
  const sentinel = useJson<SentinelResults>("/sentinel.json");
  const bench = useJson<BenchResults>("/bench.json");

  const nothing =
    toctou.missing && chaos.missing && sentinel.missing && bench.missing;

  return (
    <Box display="flex" flexDirection="column" gap="spacing.7">
      <PageHeader
        eyebrow="Evidence"
        title="What has been measured."
        trailing="And what has not."
        lede="Four claims, kept apart on purpose — they rest on very different evidence. An attack succeeds when money moves beyond the mandate, never when a gate merely returns DENY."
      />

      {nothing && (
        <EmptyState
          asset={
            <BarChartIcon size="2xlarge" color="surface.icon.gray.muted" />
          }
          title="No results on disk"
          description="Run `make console-data`. It writes toctou.json, chaos.json and sentinel.json with no keys and no network; bench.json appears only after `make bench`, which needs a model key."
          size="large"
        />
      )}

      {toctou.data && <TocTouSection results={toctou.data} />}
      {chaos.data && <ChaosSection results={chaos.data} />}
      {sentinel.data && <SentinelSection results={sentinel.data} />}
      <HaatSection results={bench.data} missing={bench.missing} />
    </Box>
  );
}

// ==================================================================== toctou

function TocTouSection({ results }: { results: TocTouResults }): JSX.Element {
  /** Worst case per (mutation, defence): the number a merchant would actually lose. */
  const worst = (mutation: string, defence: string): TocTouRun =>
    results.runs
      .filter((run) => run.mutation === mutation && run.defence === defence)
      .reduce((a, b) => (b.overspent > a.overspent ? b : a));

  const rows = results.mutations.map((mutation) => ({
    id: mutation.key,
    ...mutation,
    none: worst(mutation.key, "none"),
    prompt: worst(mutation.key, "prompt"),
    naka: worst(mutation.key, "naka"),
  }));

  return (
    <Box display="flex" flexDirection="column" gap="spacing.4">
      <Box>
        <Heading size="medium" weight="semibold">
          Price changed between reading it and paying it
        </Heading>
        <Text color="surface.text.gray.subtle" marginTop="spacing.2">
          The agent is honest throughout — it reads the page, reports{" "}
          {inr(results.listed)}, and orders exactly what the shopper asked for.
          The merchant reprices before checkout. There is no injected text, no
          model is fooled, and <b>no amount of model capability helps</b>,
          because there is no reasoning error to correct.
        </Text>
      </Box>

      <Alert
        color="information"
        title={`Prompt hardening lost ${inr(results.totals.prompt ?? 0)}; PayNaka lost ${inr(
          results.totals.naka ?? 0,
        )}`}
        description="A prompt defence has nothing to be suspicious of — the poisoned text it looks for does not exist, because this attack never needed any. A frozen mandate ends it in one comparison, and does not care why the number changed."
        isDismissible={false}
      />

      <Card padding="spacing.5">
        <CardBody>
          <Table data={{ nodes: rows }}>
            {(tableData) => (
              <>
                <TableHeader>
                  <TableHeaderRow>
                    <TableHeaderCell>Reprice</TableHeaderCell>
                    <TableHeaderCell>Charged</TableHeaderCell>
                    <TableHeaderCell>None</TableHeaderCell>
                    <TableHeaderCell>Prompt</TableHeaderCell>
                    <TableHeaderCell>PayNaka</TableHeaderCell>
                  </TableHeaderRow>
                </TableHeader>
                <TableBody>
                  {tableData.map((row) => (
                    <TableRow key={row.id} item={row}>
                      <TableCell>
                        <Box display="flex" flexDirection="column">
                          <Text weight="semibold">{row.label}</Text>
                          <Text size="small" color="surface.text.gray.muted">
                            {row.why}
                          </Text>
                        </Box>
                      </TableCell>
                      <TableCell>{inr(row.charged)}</TableCell>
                      <TableCell>
                        <Badge color="negative" emphasis="subtle">
                          {inr(row.none.overspent)}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge color="negative" emphasis="subtle">
                          {inr(row.prompt.overspent)}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Box display="flex" flexDirection="column">
                          <Badge color="positive" emphasis="subtle">
                            {inr(row.naka.overspent)}
                          </Badge>
                          {row.naka.check_id && (
                            <Text size="small" color="surface.text.gray.muted">
                              {row.naka.check_id}
                            </Text>
                          )}
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

      <Card padding="spacing.5">
        <CardBody>
          <Text size="small" color="surface.text.gray.subtle">
            The limit, stated rather than buried:{" "}
            <b>the bound is exactly as tight as the mandate</b>. These runs
            authorise the listed price to the paise, which is the strongest
            case. A shopper who says &ldquo;something under ₹2,500&rdquo; for a
            ₹1,999 bag has handed over ₹501 of room, and a +5% skim inside that
            room is <i>authorised</i> — the envelope will not stop it. In the
            bundled policy the merchant&apos;s own step-up band catches it
            instead, which is a second and separate mechanism worth
            distinguishing from the first. Run <code>make toctou</code> with{" "}
            <code>--budget 250000</code> to see it.
          </Text>
        </CardBody>
      </Card>

      <Divider />
    </Box>
  );
}

// ==================================================================== chaos

function ChaosSection({ results }: { results: ChaosResults }): JSX.Element {
  const rows = results.scenarios.map((scenario, index) => ({
    ...scenario,
    id: String(index),
  }));

  return (
    <Box display="flex" flexDirection="column" gap="spacing.4">
      <Box>
        <Heading size="medium" weight="semibold">
          Webhook delivery
        </Heading>
        <Text color="surface.text.gray.subtle" marginTop="spacing.2">
          Six ways a gateway loses a merchant money with nobody attacking
          anything. No model runs, no keys are needed, and every number
          reproduces to the paise on any machine.
        </Text>
      </Box>

      <Alert
        color={results.totals.paynaka_overspent === 0 ? "positive" : "negative"}
        title={`Naive handler overspent ${inr(results.totals.naive_overspent)}; PayNaka overspent ${inr(
          results.totals.paynaka_overspent,
        )}`}
        description="On one ₹1,999 order where a single ₹499 item came back. The naive handler is not a strawman — it checks the payment, checks the balance, and deduplicates. Under one worker with deliveries in order it is correct, and the first row says so."
        isDismissible={false}
      />

      <Card padding="spacing.5">
        <CardBody>
          <Table data={{ nodes: rows }}>
            {(tableData) => (
              <>
                <TableHeader>
                  <TableHeaderRow>
                    <TableHeaderCell>Hazard</TableHeaderCell>
                    <TableHeaderCell>Naive</TableHeaderCell>
                    <TableHeaderCell>PayNaka</TableHeaderCell>
                    <TableHeaderCell>Prevented</TableHeaderCell>
                  </TableHeaderRow>
                </TableHeader>
                <TableBody>
                  {tableData.map((row) => (
                    <TableRow key={row.id} item={row}>
                      <TableCell>
                        <Box display="flex" flexDirection="column">
                          <Text weight="semibold">{row.title}</Text>
                          <Text size="small" color="surface.text.gray.muted">
                            {row.hazard}
                          </Text>
                        </Box>
                      </TableCell>
                      <TableCell>
                        <Badge
                          color={
                            row.naive.overspent > 0 ? "negative" : "positive"
                          }
                          emphasis="subtle"
                        >
                          {inr(row.naive.left_the_gateway)}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge
                          color={
                            row.paynaka.overspent > 0 ? "negative" : "positive"
                          }
                          emphasis="subtle"
                        >
                          {inr(row.paynaka.left_the_gateway)}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Text
                          weight={row.prevented > 0 ? "semibold" : "regular"}
                        >
                          {row.prevented > 0 ? inr(row.prevented) : "—"}
                        </Text>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </>
            )}
          </Table>
        </CardBody>
      </Card>

      {results.scenarios.some((s) => s.paynaka.books_disagree !== 0) && (
        <Card padding="spacing.5">
          <CardBody>
            <Text size="small" color="surface.text.gray.subtle">
              In one scenario PayNaka&apos;s ledger sits behind the rail on
              purpose. The gateway did the work and the response was lost, so
              the outcome is genuinely unknown; the audit chain records{" "}
              <b>rail.indeterminate</b> and the balance stays claimed rather
              than the ledger inventing a number. A ledger that guesses is worse
              than one that admits it does not know.
            </Text>
          </CardBody>
        </Card>
      )}
    </Box>
  );
}

// ==================================================================== sentinel

function SentinelSection({
  results,
}: {
  results: SentinelResults;
}): JSX.Element {
  const tight = results.margin < 20;

  return (
    <Box display="flex" flexDirection="column" gap="spacing.4">
      <Divider />
      <Box>
        <Heading size="medium" weight="semibold">
          Sentinel
        </Heading>
        <Text color="surface.text.gray.subtle" marginTop="spacing.2">
          The layer-two detector, which notices poisoned fields. It is{" "}
          <b>not</b> what provides the guarantee — the gate does not import it
          and a flag never blocks anything. These numbers are its own and are
          never combined with the gate&apos;s.
        </Text>
      </Box>

      <Card padding="spacing.5">
        <CardBody>
          <Box display="flex" flexDirection="column" gap="spacing.5">
            <Box>
              <Box
                display="flex"
                justifyContent="space-between"
                marginBottom="spacing.2"
              >
                <Text weight="semibold">Recall</Text>
                <Text>
                  {(results.recall * 100).toFixed(1)}% — {results.caught}/
                  {results.attacks} payloads
                </Text>
              </Box>
              <ProgressBar value={results.recall * 100} color="positive" />
            </Box>

            <Box>
              <Box
                display="flex"
                justifyContent="space-between"
                marginBottom="spacing.2"
              >
                <Text weight="semibold">False positives</Text>
                <Text>
                  {(results.false_positive_rate * 100).toFixed(1)}% —{" "}
                  {results.false_positives}/{results.negatives} benign fields
                </Text>
              </Box>
              <ProgressBar
                value={results.false_positive_rate * 100}
                color={
                  results.false_positive_rate > 0.05 ? "negative" : "positive"
                }
              />
            </Box>
          </Box>
        </CardBody>
      </Card>

      {results.nearest_miss && (
        <Alert
          color={tight ? "notice" : "information"}
          title={`Margin: ${results.margin} points`}
          description={`A zero false-positive rate says nothing about how nearly it happened. The closest honest text scored ${results.nearest_miss.score} of ${results.nearest_miss.threshold} — ${results.nearest_miss.case_id}, matching ${results.nearest_miss.rules.join(", ")}. Recall against the sealed families is unmeasured by design: the rules were written by reading the visible corpus, and the held-out set is the only evidence any of it generalises.`}
          isDismissible={false}
        />
      )}
    </Box>
  );
}

// ==================================================================== haat

function HaatSection({
  results,
  missing,
}: {
  results: BenchResults | null;
  missing: boolean;
}): JSX.Element {
  const rows: DefenceRow[] = (results?.defences ?? []).map((row, index) => ({
    ...row,
    id: String(index),
  }));

  return (
    <Box display="flex" flexDirection="column" gap="spacing.4">
      <Divider />
      <Box>
        <Heading size="medium">HAAT — catalog injection</Heading>
        <Text color="surface.text.gray.subtle" marginTop="spacing.2">
          Four defences, one corpus, a real model. This is the only section that
          costs money to produce, and it is the one with the least to show.
        </Text>
      </Box>

      {missing && !results && (
        <Alert
          color="notice"
          title="Not run here — and the honest reason matters"
          description="bench.json is absent because `make bench` needs a model key. When it was run, across three model families on three continents and six framings each, no attack moved any money even with the gate switched OFF: the models read the poisoned review and bought what the shopper asked for. That is a measured negative result, not a bug. Publishing four rows of zeroes would look exactly like a triumph, so this section stays empty until there is something real to put in it. See docs/HAAT.md."
          isDismissible={false}
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
                            <Text
                              weight={
                                row.defence === "naka" ? "semibold" : "regular"
                              }
                            >
                              {LABELS[row.defence] ?? row.defence}
                            </Text>
                          </TableCell>
                          <TableCell>
                            <Badge
                              color={
                                row.attack_success_rate > 0.05
                                  ? "negative"
                                  : "positive"
                              }
                              emphasis="subtle"
                            >
                              {(row.attack_success_rate * 100).toFixed(1)}%
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <Badge
                              color={
                                row.benign_pass_rate < 0.95
                                  ? "notice"
                                  : "positive"
                              }
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
        </>
      )}

      <Card padding="spacing.5">
        <CardBody>
          <Text size="small" color="surface.text.gray.subtle">
            Read both columns together or not at all. A defence that refuses
            everything scores 0% attack success and 0% benign pass — that is not
            a defence, it is an outage. A model refusal is neither an attack
            success nor a defensive win, so it is counted on its own rather than
            folded into either.
          </Text>
        </CardBody>
      </Card>
    </Box>
  );
}
