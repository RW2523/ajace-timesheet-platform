import { NextResponse } from "next/server";
import crypto from "crypto";
import { query, queryOne } from "@/lib/aws/db";

export const runtime = "nodejs";

// Issues a one-hour reset token. Always returns ok (don't leak which emails exist).
// Email delivery of the link is via Amazon SES — see TODO below.
export async function POST(request) {
  const { email } = await request.json().catch(() => ({}));
  const u = await queryOne(`select id from public.auth_users where lower(email)=lower($1)`, [email || ""]);
  if (u) {
    const token = crypto.randomBytes(32).toString("hex");
    await query(
      `update public.auth_users set reset_token=$1, reset_expires=now()+interval '1 hour' where id=$2`,
      [token, u.id]
    );
    const link = `${process.env.SITE_URL || ""}/reset?token=${token}`;
    // TODO: send `link` via Amazon SES (SES client + verified sender).
    if (process.env.NODE_ENV !== "production") console.log(`[password reset] ${link}`);
  }
  return NextResponse.json({ ok: true });
}
