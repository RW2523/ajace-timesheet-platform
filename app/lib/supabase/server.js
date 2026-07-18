import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";
import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config";

// Server-side Supabase client (route handlers, server components). Reads/writes
// the auth cookies so the session is available on the server.
export async function createClient() {
  const cookieStore = await cookies();
  // Share the session cookie across *.ajace.com subdomains for SSO (set in prod only).
  const cookieDomain = process.env.NEXT_PUBLIC_COOKIE_DOMAIN;
  return createServerClient(
    SUPABASE_URL,
    SUPABASE_ANON_KEY,
    {
      ...(cookieDomain ? { cookieOptions: { domain: cookieDomain } } : {}),
      cookies: {
        getAll() {
          return cookieStore.getAll();
        },
        setAll(cookiesToSet) {
          try {
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options)
            );
          } catch {
            // called from a Server Component — safe to ignore (middleware refreshes)
          }
        },
      },
    }
  );
}
