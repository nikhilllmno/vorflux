"use client";

import { useCallback, useEffect, useState } from "react";

import { CurrentHotel } from "@/components/current-hotel";
import { HotelSwitcher } from "@/components/hotel-switcher";
import { SignInForm } from "@/components/sign-in-form";
import type { SignedInUser } from "@/lib/auth";
import type { HarnessDeps } from "@/lib/browser-deps";
import { chooseHotel, type HotelAccess, type TenantRef } from "@/lib/tenancy";

import styles from "./harness.module.css";

type AuthState =
  { status: "loading" } | { status: "signed_out" } | { status: "signed_in"; user: SignedInUser };

const SESSION_ENDED = "Your session ended. Sign in again.";

function authStateFor(user: SignedInUser | null) {
  return (prev: AuthState): AuthState => {
    if (!user) return { status: "signed_out" };
    // A token refresh reports the same user again; keep the state so nothing reloads.
    if (prev.status === "signed_in" && prev.user.id === user.id && prev.user.email === user.email) {
      return prev;
    }
    return { status: "signed_in", user };
  };
}

/** Sign-in, sign-out and the hotel switcher. All I/O goes through `deps`. */
export function HarnessApp({ deps }: { deps: HarnessDeps }) {
  const [auth, setAuth] = useState<AuthState>({ status: "loading" });
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let heardEvent = false;
    const stop = deps.auth.onUserChange((user) => {
      heardEvent = true;
      if (active) setAuth(authStateFor(user));
    });
    // An auth event that arrives first is newer than this read, so it wins.
    deps.auth.currentUser().then(
      (user) => {
        if (active && !heardEvent) setAuth(authStateFor(user));
      },
      () => {
        if (active && !heardEvent) setAuth({ status: "signed_out" });
      },
    );
    return () => {
      active = false;
      stop();
    };
  }, [deps.auth]);

  const userId = auth.status === "signed_in" ? auth.user.id : null;

  const signOut = useCallback(
    async (message: string | null) => {
      if (userId) deps.selection.clear(userId);
      setNotice(message);
      try {
        await deps.auth.signOut();
      } finally {
        setAuth({ status: "signed_out" });
      }
    },
    [deps.auth, deps.selection, userId],
  );

  const onSignOut = useCallback(() => void signOut(null), [signOut]);
  const onSessionEnded = useCallback(() => void signOut(SESSION_ENDED), [signOut]);

  const onSignIn = useCallback(
    async (email: string, password: string) => {
      const result = await deps.auth.signIn(email, password);
      if (result.ok) {
        setNotice(null);
        const user = await deps.auth.currentUser();
        setAuth(authStateFor(user));
      }
      return result;
    },
    [deps.auth],
  );

  if (auth.status === "loading") return <p className={styles.muted}>Loading…</p>;
  if (auth.status === "signed_out") return <SignInForm onSignIn={onSignIn} notice={notice} />;
  return (
    <HotelWorkspace
      key={auth.user.id}
      user={auth.user}
      deps={deps}
      onSignOut={onSignOut}
      onSessionEnded={onSessionEnded}
    />
  );
}

type ListState =
  { status: "loading" } | { status: "error" } | { status: "ready"; hotels: HotelAccess[] };

function HotelWorkspace({
  user,
  deps,
  onSignOut,
  onSessionEnded,
}: {
  user: SignedInUser;
  deps: HarnessDeps;
  onSignOut: () => void;
  onSessionEnded: () => void;
}) {
  const [list, setList] = useState<ListState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [preferred, setPreferred] = useState<TenantRef | null>(() => deps.selection.load(user.id));

  useEffect(() => {
    let active = true;
    deps.hotels.listMyHotels(user.id).then(
      (hotels) => active && setList({ status: "ready", hotels }),
      () => active && setList({ status: "error" }),
    );
    return () => {
      active = false;
    };
  }, [deps.hotels, user.id, attempt]);

  const selected = list.status === "ready" ? chooseHotel(list.hotels, preferred) : null;
  const selectedOrg = selected?.orgId;
  const selectedHotel = selected?.hotelId;

  useEffect(() => {
    if (selectedOrg && selectedHotel) {
      deps.selection.save(user.id, { orgId: selectedOrg, hotelId: selectedHotel });
    }
  }, [deps.selection, user.id, selectedOrg, selectedHotel]);

  return (
    <section className={styles.card} aria-label="Workspace">
      <div className={styles.row}>
        <p className={styles.muted}>
          Signed in as <span className={styles.strong}>{user.email ?? user.id}</span>
        </p>
        <button className={styles.secondaryButton} type="button" onClick={onSignOut}>
          Sign out
        </button>
      </div>

      {list.status === "loading" && <p className={styles.muted}>Loading your hotels…</p>}

      {list.status === "error" && (
        <div className={styles.row}>
          <p role="alert" className={styles.error}>
            Could not load your hotels.
          </p>
          <button
            className={styles.secondaryButton}
            type="button"
            onClick={() => {
              setList({ status: "loading" });
              setAttempt((n) => n + 1);
            }}
          >
            Try again
          </button>
        </div>
      )}

      {list.status === "ready" && !selected && (
        <p>Your account has no hotel access yet. Ask an admin of your organization to add you.</p>
      )}

      {list.status === "ready" && selected && (
        <>
          <HotelSwitcher
            hotels={list.hotels}
            value={selected}
            onChange={(hotel) => setPreferred({ orgId: hotel.orgId, hotelId: hotel.hotelId })}
          />
          <CurrentHotel
            key={`${selected.orgId}/${selected.hotelId}`}
            hotel={selected}
            deps={deps}
            onSessionEnded={onSessionEnded}
          />
        </>
      )}
    </section>
  );
}
