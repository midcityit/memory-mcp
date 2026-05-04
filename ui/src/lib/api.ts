const API_URL = process.env.MEMORY_API_URL || "https://ziese-memory.midcityit.com";
const API_TOKEN = process.env.MEMORY_API_TOKEN || "";

export interface Memory {
  id: string;
  type: string;
  name: string;
  content: string;
  source_repo: string;
  agent: string;
  tags: string[];
  created_at: string;
  updated_at: string;
  stale: boolean;
}

interface SearchResult extends Memory {
  score?: number;
}

async function apiFetch(path: string, init?: RequestInit) {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${API_TOKEN}`,
      "Content-Type": "application/json",
      ...init?.headers,
    },
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  return res.json();
}

export async function listMemories(params?: {
  type?: string;
  source_repo?: string;
  agent?: string;
  tags?: string;
}): Promise<Memory[]> {
  const sp = new URLSearchParams();
  if (params?.type) sp.set("type", params.type);
  if (params?.source_repo) sp.set("source_repo", params.source_repo);
  if (params?.agent) sp.set("agent", params.agent);
  if (params?.tags) sp.set("tags", params.tags);
  const qs = sp.toString();
  return apiFetch(`/memories${qs ? `?${qs}` : ""}`);
}

export async function getMemory(id: string): Promise<Memory> {
  return apiFetch(`/memories/${id}`);
}

export async function searchMemories(
  query: string,
  limit = 20,
  filter_type?: string,
  filter_source_repo?: string
): Promise<SearchResult[]> {
  return apiFetch("/memories/search", {
    method: "POST",
    body: JSON.stringify({ query, limit, filter_type, filter_source_repo }),
  });
}

export async function createMemory(data: {
  type: string;
  name: string;
  content: string;
  source_repo?: string;
  agent?: string;
  tags?: string[];
}): Promise<Memory> {
  return apiFetch("/memories", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateMemory(
  id: string,
  data: { content?: string; tags?: string[] }
): Promise<Memory> {
  return apiFetch(`/memories/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteMemory(id: string): Promise<void> {
  await apiFetch(`/memories/${id}`, { method: "DELETE" });
}

export async function deleteMemories(ids: string[]): Promise<void> {
  await Promise.all(ids.map((id) => deleteMemory(id)));
}
