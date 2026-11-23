// Which Supabase key may reach the browser. Used at run time (src/lib/config.ts) and at build
// time (next.config.ts). Next.js inlines NEXT_PUBLIC_* values into the client bundle, so a run-time
// check alone is too late: the build must stop before a secret key is bundled.
// Relative imports only: next.config.ts loads this file without the `@/` alias.

function jwtRole(key: string): unknown {
  const parts = key.split(".");
  if (parts.length !== 3) return undefined;
  try {
    const json = atob(parts[1].replace(/-/g, "+").replace(/_/g, "/"));
    const payload: unknown = JSON.parse(json);
    return typeof payload === "object" && payload !== null
      ? (payload as Record<string, unknown>).role
      : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Only a publishable key (`sb_publishable_...`) or a legacy JWT with role `anon` is browser-safe.
 * A secret key or a `service_role` JWT bypasses RLS.
 */
export function isBrowserSafeKey(key: string): boolean {
  if (key.startsWith("sb_publishable_")) return true;
  return jwtRole(key) === "anon";
}

/**
 * Throws when NEXT_PUBLIC_SUPABASE_ANON_KEY is set to a key that is not browser-safe, so
 * `next build` and `next dev` stop before the value reaches a bundle. An unset key is allowed
 * (CI builds without one; the page then says what is missing). The message never holds the value.
 */
export function assertBrowserSafeSupabaseKey(env: { NEXT_PUBLIC_SUPABASE_ANON_KEY?: string }) {
  const key = env.NEXT_PUBLIC_SUPABASE_ANON_KEY?.trim();
  if (key && !isBrowserSafeKey(key)) {
    throw new Error(
      "NEXT_PUBLIC_SUPABASE_ANON_KEY is not a publishable (or anon) key. Next.js would put it in " +
        "the browser bundle, so the build stops. Use the publishable key, never a secret or " +
        "service_role key.",
    );
  }
}
