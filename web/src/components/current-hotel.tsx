"use client";

import { useEffect, useState } from "react";

import type { HarnessDeps } from "@/lib/browser-deps";
import { HarnessApiError, type MeResponse } from "@/lib/harness-api";
import type { HotelAccess } from "@/lib/tenancy";

import styles from "./harness.module.css";

type MeState =
  | { status: "loading" }
  | { status: "ready"; me: MeResponse }
  | { status: "forbidden" }
  | { status: "unavailable" }
  | { status: "failed" };

/**
 * Asks the API for the caller's role in the chosen hotel (`GET .../me`, docs/auth.md). The parent
 * gives each hotel its own key, so a late answer for an earlier hotel never shows here.
 */
export function CurrentHotel({
  userId,
  hotel,
  deps,
  onSessionEnded,
}: {
  /** The signed-in user. An answer about anyone else is not shown. */
  userId: string;
  hotel: HotelAccess;
  deps: Pick<HarnessDeps, "auth" | "api">;
  onSessionEnded: () => void;
}) {
  const [state, setState] = useState<MeState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const { orgId, hotelId } = hotel;

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    (async () => {
      const token = await deps.auth.accessToken();
      if (signal.aborted) return;
      if (!token) {
        onSessionEnded();
        return;
      }
      try {
        const me = await deps.api.getMe({ orgId, hotelId }, token, signal);
        if (signal.aborted) return;
        // Another tab can sign in as someone else while this request is in flight; the auth
        // listener then remounts the workspace, and this answer must not show meanwhile.
        if (me.userId.toLowerCase() !== userId.toLowerCase()) setState({ status: "failed" });
        else setState({ status: "ready", me });
      } catch (error) {
        if (signal.aborted) return;
        const kind = error instanceof HarnessApiError ? error.kind : "unexpected";
        if (kind === "unauthenticated") onSessionEnded();
        else if (kind === "forbidden") setState({ status: "forbidden" });
        else if (kind === "unavailable" || kind === "network") setState({ status: "unavailable" });
        else setState({ status: "failed" });
      }
    })();
    return () => controller.abort();
  }, [deps.auth, deps.api, userId, orgId, hotelId, attempt, onSessionEnded]);

  const retry = () => {
    setState({ status: "loading" });
    setAttempt((n) => n + 1);
  };

  switch (state.status) {
    case "loading":
      return <p className={styles.muted}>Checking your access…</p>;
    case "ready":
      return (
        <p>
          You work in {hotel.hotelName} ({hotel.orgName}) as {state.me.role}.
        </p>
      );
    case "forbidden":
      return (
        <p role="alert" className={styles.error}>
          You do not have access to this hotel. Ask an admin of your organization.
        </p>
      );
    case "unavailable":
    case "failed":
      return (
        <div className={styles.row}>
          <p role="alert" className={styles.error}>
            {state.status === "unavailable"
              ? "The harness API is not available. Try again later."
              : "Something went wrong. Try again."}
          </p>
          <button className={styles.secondaryButton} type="button" onClick={retry}>
            Try again
          </button>
        </div>
      );
  }
}
