// The rule for the Supabase and harness API URLs, used at run time (src/lib/config.ts) and at
// build time (next.config.ts). Plain http is allowed only for a local host, the same hosts the
// backend allows for its JWKS URL (backend/src/harness/config.py in PR #9), so a deployed build
// never sends the password or the bearer token in clear text.
// Relative imports only: next.config.ts loads this file without the `@/` alias.

export const PUBLIC_URL_NAMES = [
  "NEXT_PUBLIC_SUPABASE_URL",
  "NEXT_PUBLIC_HARNESS_API_URL",
] as const;
export type PublicUrlName = (typeof PUBLIC_URL_NAMES)[number];

// `URL.hostname` keeps the brackets of an IPv6 address.
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

export type UrlCheck = { ok: true; url: string } | { ok: false; problem: string };

/** An http(s) URL without a trailing slash, or the reason it is refused. */
export function checkPublicUrl(name: PublicUrlName, value: string): UrlCheck {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return { ok: false, problem: `${name} must be an http(s) URL.` };
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    return { ok: false, problem: `${name} must be an http(s) URL.` };
  }
  if (url.protocol === "http:" && !LOCAL_HOSTS.has(url.hostname)) {
    return {
      ok: false,
      problem: `${name} must use https (plain http is allowed only for localhost).`,
    };
  }
  return { ok: true, url: value.replace(/\/+$/, "") };
}

/**
 * Throws when a set URL is refused, so `next build` and `next dev` stop instead of building an app
 * that would send credentials in clear text. An unset URL is allowed (CI builds without one; the
 * page then says what is missing).
 */
export function assertSecurePublicUrls(env: Partial<Record<PublicUrlName, string>>) {
  const problems = PUBLIC_URL_NAMES.flatMap((name) => {
    const value = env[name]?.trim();
    if (!value) return [];
    const checked = checkPublicUrl(name, value);
    return checked.ok ? [] : [checked.problem];
  });
  if (problems.length > 0) throw new Error(problems.join(" "));
}
