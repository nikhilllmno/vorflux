import styles from "./page.module.css";

const plannedAreas = [
  "Hotels and their agents (voice, text, email)",
  "Runs and sessions, with live streaming",
  "Approval inbox for code changes and risky actions",
  "Skills, memory and scheduled jobs per hotel",
];

export default function Home() {
  const apiUrl = process.env.NEXT_PUBLIC_HARNESS_API_URL ?? "http://localhost:8000";

  return (
    <main className={styles.main}>
      <h1>Hotel Agent Harness</h1>
      <p className={styles.muted}>
        Control-plane UI skeleton. See <span className={styles.code}>docs/architecture.md</span>{" "}
        for the design.
      </p>
      <h2>Planned areas</h2>
      <ul className={styles.list}>
        {plannedAreas.map((area) => (
          <li key={area}>{area}</li>
        ))}
      </ul>
      <p className={styles.muted}>
        API: <span className={styles.code}>{apiUrl}</span>
      </p>
    </main>
  );
}
