import type { NextConfig } from "next";

import { assertSecurePublicUrls } from "./src/lib/public-url";
import { buildSecurityHeaders } from "./src/lib/security-headers";
import { assertBrowserSafeSupabaseKey } from "./src/lib/supabase-key";

// Stop before a secret Supabase key is inlined into the client bundle (DEX-13).
assertBrowserSafeSupabaseKey({
  NEXT_PUBLIC_SUPABASE_ANON_KEY: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
});
// Stop before an app is built that would send the password or token over plain http.
assertSecurePublicUrls({
  NEXT_PUBLIC_SUPABASE_URL: process.env.NEXT_PUBLIC_SUPABASE_URL,
  NEXT_PUBLIC_HARNESS_API_URL: process.env.NEXT_PUBLIC_HARNESS_API_URL,
});

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: buildSecurityHeaders({
          supabaseUrl: process.env.NEXT_PUBLIC_SUPABASE_URL,
          harnessApiUrl: process.env.NEXT_PUBLIC_HARNESS_API_URL,
          dev: process.env.NODE_ENV === "development",
        }),
      },
    ];
  },
};

export default nextConfig;
