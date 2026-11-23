import type { NextConfig } from "next";

import { assertBrowserSafeSupabaseKey } from "./src/lib/supabase-key";

// Stop before a secret Supabase key is inlined into the client bundle (DEX-13).
assertBrowserSafeSupabaseKey({
  NEXT_PUBLIC_SUPABASE_ANON_KEY: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
});

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
};

export default nextConfig;
