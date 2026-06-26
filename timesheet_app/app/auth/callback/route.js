import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

// Handles the link Supabase emails (password recovery, email confirm).
// Exchanges the one-time code for a session cookie, then forwards the user on.
export async function GET(request) {
  const { searchParams, origin } = new URL(request.url);
  const code = searchParams.get("code");
  const next = searchParams.get("next") || "/dashboard";

  if (code) {
    const supabase = await createClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) {
      return NextResponse.redirect(`${origin}${next}`);
    }
  }
  return NextResponse.redirect(`${origin}/login?error=auth_callback`);
}
