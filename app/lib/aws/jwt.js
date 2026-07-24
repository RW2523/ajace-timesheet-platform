// Edge-safe session token (used by middleware AND node routes). `jose` only —
// no bcrypt, no next/headers — so it loads in the Edge runtime.
import { SignJWT, jwtVerify } from "jose";

export const SESSION_COOKIE = "ts_session";
const secret = () =>
  new TextEncoder().encode(
    process.env.AUTH_JWT_SECRET || "dev-insecure-change-me-please-32bytes!!"
  );

export async function signSession({ id, email, role }) {
  return new SignJWT({ email, role })
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(id)
    .setIssuedAt()
    .setExpirationTime("7d")
    .sign(secret());
}

// -> { id, email, role } or null
export async function verifySession(token) {
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, secret());
    return { id: payload.sub, email: payload.email, role: payload.role || "employee" };
  } catch {
    return null;
  }
}
