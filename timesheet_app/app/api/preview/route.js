import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { previewUpload } from "@/lib/engine";

export const maxDuration = 120;

export async function POST(request) {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) return NextResponse.json({ error: "not authenticated" }, { status: 401 });

  let form;
  try {
    form = await request.formData();
  } catch {
    return NextResponse.json({ error: "bad form data" }, { status: 400 });
  }
  const file = form.get("file");
  if (!file) return NextResponse.json({ error: "file required" }, { status: 400 });

  try {
    const result = await previewUpload(file, file.name || "upload");
    return NextResponse.json(result);
  } catch (e) {
    return NextResponse.json({ error: `preview failed: ${e.message || e}` }, { status: 502 });
  }
}
