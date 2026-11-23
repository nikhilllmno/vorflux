import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HarnessRoot } from "@/components/harness-root";

describe("HarnessRoot", () => {
  it("explains which public variables are missing instead of starting sign-in", () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "");
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "");
    vi.stubEnv("NEXT_PUBLIC_HARNESS_API_URL", "");
    render(<HarnessRoot />);

    expect(screen.getByRole("heading", { name: "Sign-in is not set up" })).toBeDefined();
    expect(screen.getByText("NEXT_PUBLIC_SUPABASE_URL is not set.")).toBeDefined();
    expect(screen.queryByLabelText("Password")).toBeNull();
    vi.unstubAllEnvs();
  });
});
