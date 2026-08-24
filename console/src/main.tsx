import React from "react";
import { createRoot } from "react-dom/client";
import { BladeProvider } from "@razorpay/blade/components";
import { bladeTheme } from "@razorpay/blade/tokens";
import "@razorpay/blade/fonts.css";
import { App } from "./App";

/**
 * PayNaka's console runs on Blade, Razorpay's own design system.
 *
 * Nothing here overrides a Blade token. The whole value of adopting a design system is
 * that the result looks like it came from the organisation that wrote it, and every
 * custom colour or spacing value spent here is value given back.
 *
 * **Light only, deliberately.** There was a dark-mode toggle and it did not work -- the
 * scheme changed in the provider while a screenful of hand-picked feedback colours did
 * not, so half the console inverted and the other half stayed put. A theme that is
 * half-applied looks broken in a way that no theme at all does not, and this is a console
 * somebody demonstrates on a projector rather than reads at night.
 */
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BladeProvider themeTokens={bladeTheme} colorScheme="light">
      <App />
    </BladeProvider>
  </React.StrictMode>,
);
