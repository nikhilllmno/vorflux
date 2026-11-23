// Response headers for every page (next.config.ts). The Supabase session, with its refresh token,
// lives in localStorage (decision W1), so the Content-Security-Policy is what limits a script
// injected by a future bug: it may only talk to this origin, Supabase and the harness API.
// Relative imports only: next.config.ts loads this file without the `@/` alias.

export interface SecurityHeaderOptions {
  supabaseUrl?: string;
  harnessApiUrl?: string;
  /** `next dev` needs 'unsafe-eval' for React's debugging features. */
  dev: boolean;
}

/** The origin of an http(s) URL, or null. Never a wildcard or another scheme. */
function origin(value: string | undefined): string | null {
  if (!value || value.includes("*")) return null;
  try {
    const url = new URL(value.trim());
    return url.protocol === "https:" || url.protocol === "http:" ? url.origin : null;
  } catch {
    return null;
  }
}

export function buildSecurityHeaders(options: SecurityHeaderOptions) {
  const connect = ["'self'", origin(options.supabaseUrl), origin(options.harnessApiUrl)].filter(
    (value, index, all): value is string => value !== null && all.indexOf(value) === index,
  );
  // The page is static (cacheComponents), so there is no per-request nonce, and Next.js inlines
  // its bootstrap scripts: 'unsafe-inline' stays until a nonce or hash policy (decision W8).
  const script = ["'self'", "'unsafe-inline'", ...(options.dev ? ["'unsafe-eval'"] : [])];
  const csp = [
    "default-src 'self'",
    `script-src ${script.join(" ")}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    `connect-src ${connect.join(" ")}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");

  return [
    { key: "Content-Security-Policy", value: csp },
    { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "X-Frame-Options", value: "DENY" },
  ];
}
