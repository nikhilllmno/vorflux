import { HarnessRoot } from "@/components/harness-root";

import styles from "./page.module.css";

export default function Home() {
  return (
    <main className={styles.main}>
      <h1>Hotel Agent Harness</h1>
      <HarnessRoot />
    </main>
  );
}
