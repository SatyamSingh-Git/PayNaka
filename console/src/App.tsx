import React from 'react';
import {
  Box,
  Badge,
  Divider,
  Heading,
  Text,
  TopNav,
  TopNavBrand,
  TopNavContent,
  TopNavActions,
  TabNav,
  TabNavItem,
  TabNavItems,
  IconButton,
  SunIcon,
  MoonIcon,
  ShieldIcon,
  ActivityIcon,
  BarChartIcon,
  HistoryIcon,
  SettingsIcon,
  CheckCircleIcon,
} from '@razorpay/blade/components';
import { api, type Health } from './api';
import { Live } from './screens/Live';
import { Benchmark } from './screens/Benchmark';
import { Replay } from './screens/Replay';
import { Policy } from './screens/Policy';
import { Operations } from './screens/Operations';

type ScreenId = 'live' | 'operations' | 'benchmark' | 'replay' | 'policy';

const NAV_ITEMS = [
  { href: '#live', title: 'Live', icon: ActivityIcon },
  { href: '#operations', title: 'Operations', icon: CheckCircleIcon },
  { href: '#benchmark', title: 'Benchmark', icon: BarChartIcon },
  { href: '#replay', title: 'Replay', icon: HistoryIcon },
  { href: '#policy', title: 'Policy', icon: SettingsIcon },
];

const idOf = (href: string | undefined): ScreenId => (href ?? '#live').slice(1) as ScreenId;

export function App({
  colorScheme,
  onToggleScheme,
}: {
  colorScheme: 'light' | 'dark';
  onToggleScheme: () => void;
}): JSX.Element {
  const [screen, setScreen] = React.useState<ScreenId>('live');
  const [health, setHealth] = React.useState<Health | null>(null);

  React.useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  return (
    <Box
      backgroundColor="surface.background.gray.subtle"
      minHeight="100vh"
      display="flex"
      flexDirection="column"
    >
      <TopNav>
        <TopNavBrand>
          <Box display="flex" alignItems="center" gap="spacing.3">
            <ShieldIcon size="large" color="interactive.icon.primary.normal" />
            <Box>
              <Heading size="small" weight="semibold">
                PayNaka
              </Heading>
              <Text size="xsmall" color="surface.text.gray.muted">
                पे-नाका · the checkpoint
              </Text>
            </Box>
          </Box>
        </TopNavBrand>

        <TopNavContent>
          {/* TabNav owns overflow: it hands back the items that fit and the ones that
              did not, so a narrow window collapses gracefully instead of clipping. */}
          <TabNav items={NAV_ITEMS}>
            {({ items }) => (
              <TabNavItems>
                {items.map((item) => (
                  <TabNavItem
                    key={item.href}
                    href={item.href}
                    title={item.title}
                    icon={item.icon}
                    isActive={screen === idOf(item.href)}
                    onClick={(event: React.MouseEvent) => {
                      event.preventDefault();
                      setScreen(idOf(item.href));
                    }}
                  />
                ))}
              </TabNavItems>
            )}
          </TabNav>
        </TopNavContent>

        <TopNavActions>
          <Box display="flex" alignItems="center" gap="spacing.4">
            {/* Never let a frame of a demo imply live money. */}
            <Badge color="notice" emphasis="subtle">
              {health?.rail === 'razorpay-test' ? 'Razorpay test mode' : 'Simulated rail'}
            </Badge>
            <IconButton
              icon={colorScheme === 'dark' ? SunIcon : MoonIcon}
              accessibilityLabel={`Switch to ${colorScheme === 'dark' ? 'light' : 'dark'} mode`}
              onClick={onToggleScheme}
              size="medium"
            />
          </Box>
        </TopNavActions>
      </TopNav>

      <Box flex="1" padding={{ base: 'spacing.5', m: 'spacing.7' }} maxWidth="1440px" width="100%" margin="auto">
        {screen === 'live' && <Live />}
        {screen === 'operations' && <Operations />}
        {screen === 'benchmark' && <Benchmark />}
        {screen === 'replay' && <Replay />}
        {screen === 'policy' && <Policy />}
      </Box>

      <Divider />
      <Box
        paddingX={{ base: 'spacing.5', m: 'spacing.7' }}
        paddingY="spacing.4"
        display="flex"
        gap="spacing.5"
        flexWrap="wrap"
      >
        <Text size="xsmall" color="surface.text.gray.muted">
          Test mode only. PayNaka refuses to start against a Razorpay live key.
        </Text>
        <Text size="xsmall" color="surface.text.gray.muted">
          merchant: {health?.merchant ?? '—'}
        </Text>
        <Text size="xsmall" color="surface.text.gray.muted">
          audit records: {health?.audit_records ?? 0}
        </Text>
      </Box>
    </Box>
  );
}
