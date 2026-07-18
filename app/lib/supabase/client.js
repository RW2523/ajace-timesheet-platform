"use client";
import { createBrowserClient } from "@supabase/ssr";
import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config";

// Browser-side Supabase client (auth + RLS-scoped queries from client components)
export function createClient() {
  const cookieDomain = process.env.NEXT_PUBLIC_COOKIE_DOMAIN;
  return createBrowserClient(
    SUPABASE_URL,
    SUPABASE_ANON_KEY,
    cookieDomain ? { cookieOptions: { domain: cookieDomain } } : undefined,
  );
}
