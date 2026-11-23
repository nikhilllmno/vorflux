// Public browser configuration. Only NEXT_PUBLIC_* values are read, and Next.js inlines them at
// build time, so each name is written out literally in `readPublicConfig`.

export interface PublicConfig {
  supabaseUrl: string;
  /** The Supabase publishable (or legacy anon) key. Safe in the browser; RLS protects the data. */
  supabaseAnonKey: string;
  harnessApiUrl: string;
}

export type ConfigResult = { ok: true; config: PublicConfig } | { ok: false; problems: string[] };

type PublicEnv = Partial<
  Record<
    "NEXT_PUBLIC_SUPABASE_URL" | "NEXT_PUBLIC_SUPABASE_ANON_KEY" | "NEXT_PUBLIC_HARNESS_API_URL",
    string | undefined
  >
>;

function httpUrl(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return value.replace(/\/+$/, "");
  } catch {
    return null;
  }
}

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
 * Only a publishable key (`sb_publishable_...`) or a legacy JWT with role `anon` may reach the
 * browser. A secret key or a `service_role` JWT bypasses RLS, so the app refuses to start with
 * one rather than ship it in the bundle by mistake.
 */
function isBrowserSafeKey(key: string): boolean {
  if (key.startsWith("sb_publishable_")) return true;
  return jwtRole(key) === "anon";
}

export function parsePublicConfig(env: PublicEnv): ConfigResult {
  const problems: string[] = [];
  const value = (name: keyof PublicEnv): string | null => {
    const v = env[name]?.trim();
    if (!v) {
      problems.push(`${name} is not set.`);
      return null;
    }
    return v;
  };

  const rawSupabaseUrl = value("NEXT_PUBLIC_SUPABASE_URL");
  const supabaseUrl = rawSupabaseUrl && httpUrl(rawSupabaseUrl);
  if (rawSupabaseUrl && !supabaseUrl)
    problems.push("NEXT_PUBLIC_SUPABASE_URL must be an http(s) URL.");

  const supabaseAnonKey = value("NEXT_PUBLIC_SUPABASE_ANON_KEY");
  if (supabaseAnonKey && !isBrowserSafeKey(supabaseAnonKey)) {
    problems.push(
      "NEXT_PUBLIC_SUPABASE_ANON_KEY must be the publishable (or anon) key, never a secret or service_role key.",
    );
  }

  const rawApiUrl = value("NEXT_PUBLIC_HARNESS_API_URL");
  const harnessApiUrl = rawApiUrl && httpUrl(rawApiUrl);
  if (rawApiUrl && !harnessApiUrl)
    problems.push("NEXT_PUBLIC_HARNESS_API_URL must be an http(s) URL.");

  if (problems.length > 0 || !supabaseUrl || !supabaseAnonKey || !harnessApiUrl) {
    return { ok: false, problems };
  }
  return { ok: true, config: { supabaseUrl, supabaseAnonKey, harnessApiUrl } };
}

export function readPublicConfig(): ConfigResult {
  return parsePublicConfig({
    NEXT_PUBLIC_SUPABASE_URL: process.env.NEXT_PUBLIC_SUPABASE_URL,
    NEXT_PUBLIC_SUPABASE_ANON_KEY: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
    NEXT_PUBLIC_HARNESS_API_URL: process.env.NEXT_PUBLIC_HARNESS_API_URL,
  });
}
