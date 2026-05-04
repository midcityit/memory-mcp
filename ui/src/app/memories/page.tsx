export const runtime = "edge";

import { listMemories, searchMemories, type Memory } from "@/lib/api";
import { MemoriesClient } from "./memories-client";

export default async function MemoriesPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string; type?: string; source_repo?: string; agent?: string; tags?: string }>;
}) {
  const params = await searchParams;
  let memories: Memory[];

  if (params.q) {
    memories = await searchMemories(params.q, 50, params.type, params.source_repo);
  } else {
    memories = await listMemories({
      type: params.type,
      source_repo: params.source_repo,
      agent: params.agent,
      tags: params.tags,
    });
  }

  // Extract unique filter values
  const types = [...new Set(memories.map((m) => m.type))].sort();
  const repos = [...new Set(memories.map((m) => m.source_repo))].sort();
  const agents = [...new Set(memories.map((m) => m.agent))].sort();
  const allTags = [...new Set(memories.flatMap((m) => m.tags))].sort();

  return (
    <MemoriesClient
      memories={memories}
      types={types}
      repos={repos}
      agents={agents}
      allTags={allTags}
      filters={params}
    />
  );
}
