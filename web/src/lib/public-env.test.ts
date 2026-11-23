// Only NEXT_PUBLIC_* values may reach the browser (DEX-13, AGENTS.md). Next.js inlines
// `process.env.NAME` at build time, so this test reads the source and refuses any other name.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..");
// NODE_ENV is set by Next.js itself and holds no secret.
const ALLOWED = /^(NEXT_PUBLIC_[A-Z0-9_]+|NODE_ENV)$/;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "test" ? [] : sourceFiles(path);
    return /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

describe("environment variables in web/src", () => {
  const files = sourceFiles(SRC);

  it("finds the app sources", () => {
    expect(files.length).toBeGreaterThan(5);
  });

  it("reads only NEXT_PUBLIC_* variables, by literal name", () => {
    const problems: string[] = [];
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const match of text.matchAll(/process\.env\.([A-Za-z0-9_]+)/g)) {
        if (!ALLOWED.test(match[1])) problems.push(`${relative(SRC, file)}: ${match[1]}`);
      }
      // Dynamic lookups and destructuring are not inlined and could pull in other names.
      if (/process\.env\s*\[|\{[^}]*\}\s*=\s*process\.env\b|import\.meta\.env/.test(text)) {
        problems.push(`${relative(SRC, file)}: dynamic env access`);
      }
    }
    expect(problems).toEqual([]);
  });
});
