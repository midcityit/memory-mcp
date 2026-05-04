export const runtime = "edge";

import { listMemories, searchMemories, type Memory } from "@/lib/api";

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q: query = "" } = await searchParams;
  let memories: Memory[] = [];
  let error: string | null = null;

  try {
    if (query) {
      memories = await searchMemories(query);
    } else {
      memories = await listMemories();
    }
  } catch (e) {
    error = "Failed to load memories.";
  }

  return (
    <main>
      <h1>Memory Twin</h1>
      <form method="get" style={{ marginBottom: "1.5rem" }}>
        <input
          name="q"
          type="search"
          placeholder="Search memories..."
          defaultValue={query}
          style={{ padding: "0.5rem", width: "300px", marginRight: "0.5rem" }}
        />
        <button type="submit" style={{ padding: "0.5rem 1rem" }}>
          Search
        </button>
      </form>

      {error ? (
        <p style={{ color: "red" }}>{error}</p>
      ) : (
        <>
          <p style={{ color: "#666" }}>{memories.length} memories</p>
          <ul style={{ listStyle: "none", padding: 0 }}>
            {memories.map((m) => (
              <li
                key={m.id}
                style={{ borderBottom: "1px solid #eee", padding: "0.75rem 0" }}
              >
                <strong>{m.name}</strong>
                {m.stale && (
                  <span style={{ color: "orange", marginLeft: "0.5rem" }}>
                    ⚠ stale
                  </span>
                )}
                <br />
                <small style={{ color: "#888" }}>
                  {m.type} · {m.source_repo} · {m.agent} ·{" "}
                  {new Date(m.updated_at).toLocaleDateString()}
                </small>
                {m.tags.length > 0 && (
                  <div style={{ marginTop: "0.25rem" }}>
                    {m.tags.map((t) => (
                      <span
                        key={t}
                        style={{
                          background: "#f0f0f0",
                          padding: "0.1rem 0.4rem",
                          borderRadius: "3px",
                          marginRight: "0.25rem",
                          fontSize: "0.8rem",
                        }}
                      >
                        {t}
                      </span>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </main>
  );
}
