"use client";

import { useMemo, useSyncExternalStore } from "react";

import { HarnessApp } from "@/components/harness-app";
import { createBrowserDeps, type HarnessDeps } from "@/lib/browser-deps";
import { readPublicConfig, type PublicConfig } from "@/lib/config";

import styles from "./harness.module.css";

const subscribeNever = () => () => {};

// One Supabase client per page load, also when React renders twice in development.
let cached: { key: string; deps: HarnessDeps } | null = null;

function browserDeps(config: PublicConfig): HarnessDeps {
  const key = JSON.stringify(config);
  if (cached?.key !== key) cached = { key, deps: createBrowserDeps(config) };
  return cached.deps;
}

function ConfiguredApp({ config }: { config: PublicConfig }) {
  // The session lives in browser storage, so the server render and the first client render
  // both show the loading state, and the app starts after hydration.
  const inBrowser = useSyncExternalStore(
    subscribeNever,
    () => true,
    () => false,
  );
  if (!inBrowser) return <p className={styles.muted}>Loading…</p>;
  return <HarnessApp deps={browserDeps(config)} />;
}

export function HarnessRoot() {
  const result = useMemo(() => readPublicConfig(), []);
  if (result.ok) return <ConfiguredApp config={result.config} />;
  return (
    <section className={styles.card} aria-labelledby="setup-heading">
      <h2 id="setup-heading">Sign-in is not set up</h2>
      <p className={styles.muted}>
        Set these variables for the web app (names are in <code>.env.example</code>), then build
        again:
      </p>
      <ul className={styles.list}>
        {result.problems.map((problem) => (
          <li key={problem}>{problem}</li>
        ))}
      </ul>
    </section>
  );
}
