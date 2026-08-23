import React from 'react';
import { createRoot } from 'react-dom/client';
import { BladeProvider } from '@razorpay/blade/components';
import { bladeTheme } from '@razorpay/blade/tokens';
import '@razorpay/blade/fonts.css';
import { App } from './App';

/**
 * PayNaka's console runs on Blade, Razorpay's own design system.
 *
 * Nothing here overrides a Blade token. The whole value of adopting a design system is
 * that the result looks like it came from the organisation that wrote it, and every
 * custom colour or spacing value spent here is value given back.
 */
const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;

function Root(): JSX.Element {
  const [colorScheme, setColorScheme] = React.useState<'light' | 'dark'>(
    prefersDark ? 'dark' : 'light',
  );

  return (
    <BladeProvider themeTokens={bladeTheme} colorScheme={colorScheme}>
      <App colorScheme={colorScheme} onToggleScheme={() => setColorScheme((s) => (s === 'dark' ? 'light' : 'dark'))} />
    </BladeProvider>
  );
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
