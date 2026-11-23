import { describe, expect, it, vi } from "vitest";

import { createSupabaseAuthGateway, type SupabaseAuthLike } from "@/lib/auth";
import { USER_ID } from "@/test/fixtures";

type SessionLike = NonNullable<Awaited<ReturnType<SupabaseAuthLike["getSession"]>>["data"]["session"]>;

function session(overrides: Partial<SessionLike["user"]> = {}, token = "access-1"): SessionLike {
  return { access_token: token, user: { id: USER_ID, email: "staff@example.test", ...overrides } };
}

function fakeSupabaseAuth(initial: SessionLike | null = null) {
  let current = initial;
  let listener: ((event: string, s: SessionLike | null) => void) | null = null;
  const unsubscribe = vi.fn();
  const auth = {
    getSession: vi.fn(async () => ({ data: { session: current }, error: null })),
    onAuthStateChange: vi.fn((cb: (event: string, s: SessionLike | null) => void) => {
      listener = cb;
      return { data: { subscription: { unsubscribe } } };
    }),
    signInWithPassword: vi.fn(
      async (credentials: { email: string; password: string }): Promise<{
        error: { status?: number; code?: string; message: string } | null;
      }> => {
        void credentials;
        return { error: null };
      },
    ),
    signOut: vi.fn(async (options: { scope: "local" }) => {
      void options;
      return { error: null };
    }),
  } satisfies SupabaseAuthLike;
  return {
    auth,
    unsubscribe,
    setSession: (s: SessionLike | null) => (current = s),
    fire: (event: string, s: SessionLike | null) => listener?.(event, s),
  };
}

describe("createSupabaseAuthGateway", () => {
  it("signs in with email and password and trims the email", async () => {
    const fake = fakeSupabaseAuth();
    const result = await createSupabaseAuthGateway(fake.auth).signIn("  staff@example.test ", "pw");
    expect(result).toEqual({ ok: true });
    expect(fake.auth.signInWithPassword).toHaveBeenCalledWith({
      email: "staff@example.test",
      password: "pw",
    });
  });

  it.each([
    [{ status: 400, code: "invalid_credentials", message: "Invalid login credentials" }, "invalid_credentials"],
    [{ status: 429, code: "over_request_rate_limit", message: "rate" }, "rate_limited"],
    [{ status: 500, message: "boom" }, "unavailable"],
    [{ message: "fetch failed" }, "unavailable"],
  ])("maps a Supabase error %o to %s", async (error, reason) => {
    const fake = fakeSupabaseAuth();
    fake.auth.signInWithPassword.mockResolvedValueOnce({ error });
    expect(await createSupabaseAuthGateway(fake.auth).signIn("a@b.test", "pw")).toEqual({
      ok: false,
      reason,
    });
  });

  it("maps a thrown network error to unavailable", async () => {
    const fake = fakeSupabaseAuth();
    fake.auth.signInWithPassword.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    expect(await createSupabaseAuthGateway(fake.auth).signIn("a@b.test", "pw")).toEqual({
      ok: false,
      reason: "unavailable",
    });
  });

  it("signs out on this device only", async () => {
    const fake = fakeSupabaseAuth(session());
    await createSupabaseAuthGateway(fake.auth).signOut();
    expect(fake.auth.signOut).toHaveBeenCalledWith({ scope: "local" });
  });

  it("returns the current user and the current access token", async () => {
    const fake = fakeSupabaseAuth(session());
    const gateway = createSupabaseAuthGateway(fake.auth);
    expect(await gateway.currentUser()).toEqual({ id: USER_ID, email: "staff@example.test" });
    expect(await gateway.accessToken()).toBe("access-1");

    // The token is read again on every call, so a refreshed token is used at once.
    fake.setSession(session({}, "access-2"));
    expect(await gateway.accessToken()).toBe("access-2");
  });

  it("treats no session, an anonymous session and a malformed user as signed out", async () => {
    for (const s of [null, session({ is_anonymous: true }), session({ id: "not-a-uuid" })]) {
      const gateway = createSupabaseAuthGateway(fakeSupabaseAuth(s).auth);
      expect(await gateway.currentUser()).toBeNull();
      expect(await gateway.accessToken()).toBeNull();
    }
  });

  it("reports sign-in and sign-out events and unsubscribes", () => {
    const fake = fakeSupabaseAuth();
    const listener = vi.fn();
    const stop = createSupabaseAuthGateway(fake.auth).onUserChange(listener);

    fake.fire("SIGNED_IN", session());
    fake.fire("SIGNED_OUT", null);
    expect(listener.mock.calls).toEqual([[{ id: USER_ID, email: "staff@example.test" }], [null]]);

    stop();
    expect(fake.unsubscribe).toHaveBeenCalledOnce();
  });
});
