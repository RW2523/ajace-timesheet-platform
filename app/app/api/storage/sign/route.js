import { NextResponse } from "next/server";
import { currentUser } from "@/lib/aws/auth";
import { signedGetUrl, signedPutUrl } from "@/lib/aws/storage";

export const runtime = "nodejs";

// Presigned S3 URL. Owners may read/write their own folder ({userId}/...);
// admins may read anything. The first path segment is the owner id.
export async function POST(request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "not authenticated" }, { status: 401 });

  const { path, method = "get", contentType } = await request.json().catch(() => ({}));
  if (!path) return NextResponse.json({ error: "path required" }, { status: 400 });

  const owner = String(path).split("/")[0];
  const isAdmin = user.role === "admin";
  if (method === "put" && owner !== user.id) return NextResponse.json({ error: "forbidden" }, { status: 403 });
  if (method === "get" && owner !== user.id && !isAdmin) return NextResponse.json({ error: "forbidden" }, { status: 403 });

  const url = method === "put"
    ? await signedPutUrl(path, contentType || "application/octet-stream")
    : await signedGetUrl(path);
  return NextResponse.json({ url });
}
