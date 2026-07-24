import { NextResponse } from "next/server";
import crypto from "crypto";
import { query, queryOne } from "@/lib/aws/db";
import { sendPasswordReset, emailEnabled } from "@/lib/aws/email";

export const runtime = "nodejs";

// Issues a one-hour reset token and emails the link via Amazon SES.
// Always returns ok (don't leak which emails exist). If SES isn't configured,
// falls back to logging the link so the flow still works in dev.
export async function POST(request) {
  const { email } = await request.json().catch(() => ({}));
  const u = await queryOne(`select id, email from public.auth_users where lower(email)=lower($1)`, [email || ""]);
  if (u) {
    const token = crypto.randomBytes(32).toString("hex");
    await query(
      `update public.auth_users set reset_token=$1, reset_expires=now()+interval '1 hour' where id=$2`,
      [token, u.id]
    );
    const link = `${process.env.SITE_URL || ""}/reset?token=${token}`;
    try {
      if (emailEnabled()) {
        await sendPasswordReset(u.email, link);
      } else if (process.env.NODE_ENV !== "production") {
        console.log(`[password reset] ${link}`);
      }
    } catch (e) {
      // don't fail the request if email delivery hiccups; log for the operator
      console.error("SES send failed:", e?.message || e);
    }
  }
  return NextResponse.json({ ok: true });
}
