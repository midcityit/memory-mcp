"use server";

import { createMemory, updateMemory, deleteMemory, deleteMemories } from "@/lib/api";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

export async function createMemoryAction(formData: FormData) {
  const tags = (formData.get("tags") as string || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

  await createMemory({
    type: formData.get("type") as string,
    name: formData.get("name") as string,
    content: formData.get("content") as string,
    source_repo: (formData.get("source_repo") as string) || undefined,
    agent: (formData.get("agent") as string) || undefined,
    tags,
  });

  revalidatePath("/memories");
  redirect("/memories");
}

export async function updateMemoryAction(id: string, formData: FormData) {
  const tags = (formData.get("tags") as string || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

  await updateMemory(id, {
    content: formData.get("content") as string,
    tags,
  });

  revalidatePath(`/memories/${id}`);
  revalidatePath("/memories");
  redirect(`/memories/${id}`);
}

export async function deleteMemoryAction(id: string) {
  await deleteMemory(id);
  revalidatePath("/memories");
  redirect("/memories");
}

export async function bulkDeleteAction(ids: string[]) {
  await deleteMemories(ids);
  revalidatePath("/memories");
}
