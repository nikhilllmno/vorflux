"use client";

import { useState, type FormEvent } from "react";

import type { SignInFailure, SignInResult } from "@/lib/auth";

import styles from "./harness.module.css";

const FAILURE_TEXT: Record<SignInFailure, string> = {
  invalid_credentials: "The email or password is not correct.",
  rate_limited: "Too many sign-in attempts. Wait a minute and try again.",
  unavailable: "Sign-in is not available now. Try again later.",
};

export function SignInForm({
  onSignIn,
  notice,
}: {
  onSignIn: (email: string, password: string) => Promise<SignInResult>;
  notice: string | null;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      const result = await onSignIn(email, password);
      if (!result.ok) {
        setError(FAILURE_TEXT[result.reason]);
        setPassword("");
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <section className={styles.card} aria-labelledby="sign-in-heading">
      <h2 id="sign-in-heading">Sign in</h2>
      {notice && (
        <p role="status" className={styles.notice}>
          {notice}
        </p>
      )}
      <form className={styles.form} onSubmit={submit}>
        <fieldset className={styles.fieldset} disabled={pending}>
          <label className={styles.field}>
            <span>Email</span>
            <input
              className={styles.input}
              type="email"
              name="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label className={styles.field}>
            <span>Password</span>
            <input
              className={styles.input}
              type="password"
              name="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error && (
            <p role="alert" className={styles.error}>
              {error}
            </p>
          )}
          <button className={styles.button} type="submit" disabled={pending}>
            {pending ? "Signing in…" : "Sign in"}
          </button>
        </fieldset>
      </form>
      <p className={styles.muted}>Staff accounts are created by an admin of your organization.</p>
    </section>
  );
}
