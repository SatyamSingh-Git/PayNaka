/**
 * The console's shared furniture.
 *
 * Every screen was laid out by hand and it showed: five different heading sizes, five
 * different gaps between sections, and explanatory paragraphs in small grey type that made
 * each page read like a document rather than a product.
 *
 * Razorpay's own surfaces are the reference — one enormous headline, white cards on a pale
 * ground, generous space, and very few words. These three components are that, so the pages
 * differ in what they say and never in how they are built.
 */

import React from "react";
import {
  Amount,
  Badge,
  Box,
  Card,
  CardBody,
  Heading,
  Text,
} from "@razorpay/blade/components";

export type Tone = "positive" | "negative" | "notice" | undefined;

const TONE_COLOUR = {
  positive: "feedback.text.positive.intense",
  negative: "feedback.text.negative.intense",
  notice: "feedback.text.notice.intense",
} as const;

export const toneColour = (tone: Tone): string =>
  tone ? TONE_COLOUR[tone] : "surface.text.gray.normal";

/**
 * The top of a page: a small badge, one large claim, one line explaining it.
 *
 * ``title`` and ``trailing`` render at the same size with different weights — the
 * two-tone headline Razorpay uses, where the second half is the qualifier.
 */
export function PageHeader({
  eyebrow,
  title,
  trailing,
  lede,
}: {
  eyebrow?: string;
  title: string;
  trailing?: string;
  lede?: string;
}): JSX.Element {
  return (
    <Box>
      {eyebrow ? (
        <Box marginBottom="spacing.4">
          <Badge color="information" emphasis="subtle" size="medium">
            {eyebrow}
          </Badge>
        </Box>
      ) : null}
      <Box maxWidth="880px">
        <Heading size="2xlarge" weight="semibold">
          {title}
        </Heading>
        {trailing ? (
          <Box marginTop="spacing.2">
            <Heading
              size="2xlarge"
              weight="regular"
              color="surface.text.gray.muted"
            >
              {trailing}
            </Heading>
          </Box>
        ) : null}
      </Box>
      {lede ? (
        <Box marginTop="spacing.4" maxWidth="720px">
          <Text size="large" color="surface.text.gray.subtle">
            {lede}
          </Text>
        </Box>
      ) : null}
    </Box>
  );
}

/** A titled block with room above it. One line of context, never a paragraph. */
export function Section({
  title,
  caption,
  children,
}: {
  title: string;
  caption?: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <Box marginTop="spacing.10">
      <Heading size="medium" weight="semibold">
        {title}
      </Heading>
      {caption ? (
        <Box marginTop="spacing.2" maxWidth="760px">
          <Text size="medium" color="surface.text.gray.muted">
            {caption}
          </Text>
        </Box>
      ) : null}
      <Box marginTop="spacing.6">{children}</Box>
    </Box>
  );
}

/**
 * One number, sized to be read from the back of a room.
 *
 * Money goes through Blade's ``Amount`` rather than a hand-rolled formatter: it is
 * Razorpay's own component, it knows the locale's digit grouping, and it renders the symbol
 * and decimals subtly. Formatting rupees by hand was the single thing making this console
 * look like it came from somewhere else.
 */
export function Stat({
  label,
  value,
  amount,
  tone,
  hint,
  width = "232px",
}: {
  label: string;
  value?: string;
  amount?: number;
  tone?: Tone;
  hint?: string;
  width?: string;
}): JSX.Element {
  const colour = toneColour(tone);
  return (
    <Card
      elevation="lowRaised"
      padding="spacing.5" // Blade types width as a token union; these are plain CSS lengths, which it accepts
      // at runtime. Cast once here rather than at every call site.
      width={{ base: "100%", m: width } as never}
    >
      <CardBody>
        <Text size="small" weight="medium" color="surface.text.gray.muted">
          {label}
        </Text>
        <Box marginTop="spacing.3" marginBottom="spacing.2">
          {amount !== undefined ? (
            <Amount
              value={amount / 100}
              type="heading"
              size="2xlarge"
              weight="semibold"
              color={colour as never}
            />
          ) : (
            <Heading size="large" weight="semibold" color={colour as never}>
              {value}
            </Heading>
          )}
        </Box>
        {hint ? (
          <Text size="xsmall" color="surface.text.gray.muted">
            {hint}
          </Text>
        ) : null}
      </CardBody>
    </Card>
  );
}
