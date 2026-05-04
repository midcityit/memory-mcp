const API_URL = process.env.MEMORY_API_URL || "https://ziese-memory.midcityit.com";
const API_TOKEN = process.env.MEMORY_API_TOKEN || "";

interface Memory {
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

async function apiFetch(path: string, init?: RequestInit) {
  return fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${API_TOKEN}`,
      ...init?.headers,
    },
  });
}

export async function listMemories(params?: Record<string, string>): Promise<Memory[]> {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  const res = await apiFetch(`/memories${qs}`);
  if (!res.ok) return [];
  return res.json();
}

export async function searchMemories(query: string, limit = 20): Promise<Memory[]> {
  const res = await apiFetch("/memories/search", {
    method: "POST",
    body: JSON.stringify({ query, limit }),
  });
  if (!res.ok) return [];
  return res.json();
}

export type { Memory };
