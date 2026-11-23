// Sign-in and sign-out with Supabase Auth (email and password). The session lives in the browser
// (supabase-js storage); the Next.js server never sees it. The API verifies the access token
// itself (docs/auth.md), so this module only decides what the UI shows.
import { isUuid } from "@/lib/tenancy";

export interface SignedInUser {
  id: string;
  email: string | null;
}

export type SignInFailure = "invalid_credentials" | "rate_limited" | "unavailable";
export type SignInResult = { ok: true } | { ok: false; reason: SignInFailure };

export interface AuthGateway {
  currentUser(): Promise<SignedInUser | null>;
  /** Calls the listener on every sign-in, sign-out and token refresh. Returns an unsubscribe. */
  onUserChange(listener: (user: SignedInUser | null) => void): () => void;
  signIn(email: string, password: string): Promise<SignInResult>;
  /** Ends the session on this device only. */
  signOut(): Promise<void>;
  /** The current access token, read again on each call so a refreshed token is used. */
  accessToken(): Promise<string | null>;
}

interface SessionLike {
  access_token: string;
  user: { id: string; email?: string; is_anonymous?: boolean };
}

interface AuthErrorLike {
  status?: number;
  code?: string;
  message: string;
}

/** The part of `SupabaseClient["auth"]` this module uses. */
export interface SupabaseAuthLike {
  getSession(): Promise<{ data: { session: SessionLike | null }; error: AuthErrorLike | null }>;
  onAuthStateChange(callback: (event: string, session: SessionLike | null) => void): {
    data: { subscription: { unsubscribe(): void } };
  };
  signInWithPassword(credentials: {
    email: string;
    password: string;
  }): Promise<{ error: AuthErrorLike | null }>;
  signOut(options: { scope: "local" }): Promise<{ error: AuthErrorLike | null }>;
}

/** A usable staff session: a real user id and not an anonymous sign-in (the API refuses those). */
function usable(session: SessionLike | null): SessionLike | null {
  if (!session || !session.access_token) return null;
  if (session.user.is_anonymous === true || !isUuid(session.user.id)) return null;
  return session;
}

function toUser(session: SessionLike | null): SignedInUser | null {
  const s = usable(session);
  return s ? { id: s.user.id.toLowerCase(), email: s.user.email ?? null } : null;
}

function failureReason(error: AuthErrorLike): SignInFailure {
  if (error.status === 429 || error.code?.startsWith("over_")) return "rate_limited";
  if (error.status !== undefined && error.status >= 400 && error.status < 500) {
    return "invalid_credentials";
  }
  return "unavailable";
}

export function createSupabaseAuthGateway(auth: SupabaseAuthLike): AuthGateway {
  async function session(): Promise<SessionLike | null> {
    const { data } = await auth.getSession();
    return usable(data.session);
  }

  return {
    async currentUser() {
      return toUser(await session());
    },
    onUserChange(listener) {
      const { data } = auth.onAuthStateChange((_event, s) => listener(toUser(s)));
      return () => data.subscription.unsubscribe();
    },
    async signIn(email, password) {
      try {
        const { error } = await auth.signInWithPassword({ email: email.trim(), password });
        return error ? { ok: false, reason: failureReason(error) } : { ok: true };
      } catch {
        return { ok: false, reason: "unavailable" };
      }
    },
    async signOut() {
      // A failed revoke call still clears the local session in supabase-js.
      await auth.signOut({ scope: "local" });
    },
    async accessToken() {
      return (await session())?.access_token ?? null;
    },
  };
}
