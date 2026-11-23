// Runs the real supabase-js client and the real API client against a mocked `fetch`, to check
// the requests that leave the browser: the Supabase Data API query shape and that every call
// after sign-in carries the user's access token, not the publishable key.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createBrowserDeps } from "@/lib/browser-deps";
import { HOTEL_A1, ORG_A, USER_ID } from "@/test/fixtures";

const SUPABASE_URL = "https://project.supabase.example";
const API_URL = "https://api.example.test";
const PUBLISHABLE_KEY = ["sb", "publishable", "test0000"].join("_");

// Built at run time so no token-shaped literal sits in the repository.
function fakeAccessToken() {
  const part = (value: object) =>
    btoa(JSON.stringify(value)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  const now = Math.floor(Date.now() / 1000);
  return [
    part({ alg: "ES256", typ: "JWT", kid: "test" }),
    part({ sub: USER_ID, role: "authenticated", aud: "authenticated", exp: now + 3600, iat: now }),
    "signature",
  ].join(".");
}

type Seen = { url: URL; method: string; headers: Headers; body: string | null };

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("createBrowserDeps (real clients, mocked network)", () => {
  const token = fakeAccessToken();
  let seen: Seen[];
  let logoutAnswer: "ok" | "network" | 500 | 401;

  beforeEach(() => {
    seen = [];
    logoutAnswer = "ok";
    window.localStorage.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new Request(input, init);
        const url = new URL(request.url);
        seen.push({
          url,
          method: request.method,
          headers: request.headers,
          body: init?.body ? String(init.body) : null,
        });
        if (url.pathname === "/auth/v1/token") {
          return json({
            access_token: token,
            token_type: "bearer",
            expires_in: 3600,
            expires_at: Math.floor(Date.now() / 1000) + 3600,
            refresh_token: "refresh-test",
            user: { id: USER_ID, email: "staff@example.test", aud: "authenticated" },
          });
        }
        if (url.pathname === "/auth/v1/logout") {
          if (logoutAnswer === "network") throw new TypeError("Failed to fetch");
          if (logoutAnswer === "ok") return new Response(null, { status: 204 });
          return json({ message: "logout failed" }, logoutAnswer);
        }
        // Like PostgREST: a page past the end is empty.
        if (url.pathname.startsWith("/rest/v1/") && Number(url.searchParams.get("offset")) > 0) {
          return json([]);
        }
        if (url.pathname === "/rest/v1/memberships") {
          return json([{ user_id: USER_ID, org_id: ORG_A, hotel_id: HOTEL_A1, role: "operator" }]);
        }
        if (url.pathname === "/rest/v1/hotels") {
          return json([
            {
              id: HOTEL_A1,
              org_id: ORG_A,
              slug: "acme-beach",
              name: "Acme Beach",
              orgs: { id: ORG_A, slug: "acme", name: "Acme Hotels" },
            },
          ]);
        }
        if (url.origin === API_URL) {
          return json({ user_id: USER_ID, org_id: ORG_A, hotel_id: HOTEL_A1, role: "operator" });
        }
        return json({ message: "unexpected request" }, 500);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function deps() {
    return createBrowserDeps({
      supabaseUrl: SUPABASE_URL,
      supabaseAnonKey: PUBLISHABLE_KEY,
      harnessApiUrl: API_URL,
    });
  }

  it("signs in, then reads the hotel list and calls the API with the user's JWT", async () => {
    const d = deps();
    expect(await d.auth.signIn("staff@example.test", "pw")).toEqual({ ok: true });
    const signIn = seen.find((s) => s.url.pathname === "/auth/v1/token");
    expect(signIn?.url.searchParams.get("grant_type")).toBe("password");

    expect(await d.auth.currentUser()).toEqual({ id: USER_ID, email: "staff@example.test" });
    const hotels = await d.hotels.listMyHotels(USER_ID);
    expect(hotels.map((h) => [h.orgId, h.hotelId, h.role])).toEqual([
      [ORG_A, HOTEL_A1, "operator"],
    ]);

    const memberships = seen.find((s) => s.url.pathname === "/rest/v1/memberships");
    expect(memberships?.url.searchParams.get("user_id")).toBe(`eq.${USER_ID}`);
    expect(memberships?.headers.get("Authorization")).toBe(`Bearer ${token}`);
    const hotelQuery = seen.find((s) => s.url.pathname === "/rest/v1/hotels");
    expect(hotelQuery?.url.searchParams.get("org_id")).toBe(`in.(${ORG_A})`);
    expect(hotelQuery?.headers.get("Authorization")).toBe(`Bearer ${token}`);
    // Paged reads in a fixed order: offset 0 then 1 (empty), sorted by id.
    const hotelPages = seen.filter((s) => s.url.pathname === "/rest/v1/hotels");
    expect(hotelPages.map((s) => s.url.searchParams.get("offset") ?? "0")).toEqual(["0", "1"]);
    expect(hotelPages.every((s) => s.url.searchParams.get("order") === "id.asc")).toBe(true);

    const accessToken = await d.auth.accessToken();
    expect(accessToken).toBe(token);
    await d.api.getMe({ orgId: ORG_A, hotelId: HOTEL_A1 }, accessToken ?? "");
    const me = seen.find((s) => s.url.origin === API_URL);
    expect(me?.url.pathname).toBe(`/v1/orgs/${ORG_A}/hotels/${HOTEL_A1}/me`);
    expect(me?.headers.get("Authorization")).toBe(`Bearer ${token}`);
  });

  it("signs out locally and forgets the token", async () => {
    const d = deps();
    await d.auth.signIn("staff@example.test", "pw");
    await d.auth.signOut();

    const logout = seen.find((s) => s.url.pathname === "/auth/v1/logout");
    expect(logout?.url.searchParams.get("scope")).toBe("local");
    expect(await d.auth.currentUser()).toBeNull();
    expect(await d.auth.accessToken()).toBeNull();
  });

  // auth.ts relies on supabase-js removing the local session even when the revoke call fails.
  // That behaviour changed across supabase-js versions, so check it with the real client.
  it.each(["network", 500, 401] as const)(
    "removes the local session when the logout request fails (%s)",
    async (failure) => {
      const d = deps();
      await d.auth.signIn("staff@example.test", "pw");
      expect(Object.keys(window.localStorage).some((k) => k.startsWith("sb-"))).toBe(true);

      logoutAnswer = failure;
      await d.auth.signOut();

      expect(seen.some((s) => s.url.pathname === "/auth/v1/logout")).toBe(true);
      expect(Object.keys(window.localStorage).filter((k) => k.startsWith("sb-"))).toEqual([]);
      expect(await d.auth.currentUser()).toBeNull();
      expect(await d.auth.accessToken()).toBeNull();
    },
  );
});
