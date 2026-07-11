import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { previewUpload } from "@/lib/engine";

export const maxDuration = 120;

// Admin-only: download a stored source file and render it to page images (any
// format -> PDF -> PNGs via the engine) so an admin can verify a submission
// against the original document.
export async function POST(request) {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) return NextResponse.json({ error: "not authenticated" }, { status: 401 });

  const { data: prof } = await supabase
    .from("ts_profiles").select("role").eq("id", user.id).single();
  if (prof?.role !== "admin") {
    return NextResponse.json({ error: "admin only" }, { status: 403 });
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "bad body" }, { status: 400 });
  }
  const path = body?.path;
  if (!path) return NextResponse.json({ error: "path required" }, { status: 400 });

  const { data: blob, error } = await supabase.storage.from("ts-uploads").download(path);
  if (error || !blob) {
    return NextResponse.json({ error: "file not found in storage" }, { status: 404 });
  }

  try {
    const fileName = path.split("/").pop() || "document";
    const result = await previewUpload(blob, fileName);
    return NextResponse.json(result);
  } catch (e) {
    return NextResponse.json({ error: `preview failed: ${e.message || e}` }, { status: 502 });
  }
}
