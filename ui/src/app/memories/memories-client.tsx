"use client";

import { useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { Memory } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { bulkDeleteAction } from "@/app/actions";
import { Search, Plus, Trash2, Download, Clock, AlertTriangle } from "lucide-react";

function timeAgo(date: string) {
  const s = Math.floor((Date.now() - new Date(date).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function MemoriesClient({
  memories,
  types,
  repos,
  agents,
  allTags,
  filters,
}: {
  memories: Memory[];
  types: string[];
  repos: string[];
  agents: string[];
  allTags: string[];
  filters: { q?: string; type?: string; source_repo?: string; agent?: string; tags?: string };
}) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [isPending, startTransition] = useTransition();
  const [query, setQuery] = useState(filters.q || "");

  function applyFilters(updates: Record<string, string>) {
    const sp = new URLSearchParams();
    const merged = { ...filters, ...updates };
    Object.entries(merged).forEach(([k, v]) => {
      if (v) sp.set(k, v);
    });
    router.push(`/memories?${sp.toString()}`);
  }

  function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    applyFilters({ q: query });
  }

  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected((prev) =>
      prev.size === memories.length ? new Set() : new Set(memories.map((m) => m.id))
    );
  }

  function handleBulkDelete() {
    if (!confirm(`Delete ${selected.size} memories?`)) return;
    startTransition(async () => {
      await bulkDeleteAction([...selected]);
      setSelected(new Set());
    });
  }

  function handleExport() {
    const data = memories.filter((m) => selected.size === 0 || selected.has(m.id));
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `memories-export-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-4">
      {/* Search + Actions */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <form onSubmit={handleSearch} className="flex flex-1 gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
            <Input
              placeholder="Semantic search..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="pl-9"
            />
          </div>
          <Button type="submit">Search</Button>
        </form>
        <div className="flex gap-2">
          <Link href="/memories/new">
            <Button><Plus className="h-4 w-4" /> New</Button>
          </Link>
          {selected.size > 0 && (
            <Button variant="destructive" onClick={handleBulkDelete} disabled={isPending}>
              <Trash2 className="h-4 w-4" /> Delete ({selected.size})
            </Button>
          )}
          <Button variant="outline" onClick={handleExport}>
            <Download className="h-4 w-4" /> Export
          </Button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-2">
        <Select value={filters.type || ""} onChange={(e) => applyFilters({ type: e.target.value })} className="w-auto">
          <option value="">All types</option>
          {types.map((t) => <option key={t} value={t}>{t}</option>)}
        </Select>
        <Select value={filters.source_repo || ""} onChange={(e) => applyFilters({ source_repo: e.target.value })} className="w-auto">
          <option value="">All repos</option>
          {repos.map((r) => <option key={r} value={r}>{r}</option>)}
        </Select>
        <Select value={filters.agent || ""} onChange={(e) => applyFilters({ agent: e.target.value })} className="w-auto">
          <option value="">All agents</option>
          {agents.map((a) => <option key={a} value={a}>{a}</option>)}
        </Select>
        {filters.q && (
          <Button variant="ghost" size="sm" onClick={() => { setQuery(""); applyFilters({ q: "" }); }}>
            Clear search
          </Button>
        )}
      </div>

      {/* Results count */}
      <div className="flex items-center justify-between text-sm text-gray-500 dark:text-gray-400">
        <span>{memories.length} memories{filters.q ? ` matching "${filters.q}"` : ""}</span>
        {memories.length > 0 && (
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={selected.size === memories.length} onChange={toggleAll} className="rounded" />
            Select all
          </label>
        )}
      </div>

      {/* Memory list */}
      <div className="space-y-2">
        {memories.map((m) => (
          <div
            key={m.id}
            className="group flex items-start gap-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-4 hover:border-blue-300 dark:hover:border-blue-700 transition-colors"
          >
            <input
              type="checkbox"
              checked={selected.has(m.id)}
              onChange={() => toggleSelect(m.id)}
              className="mt-1 rounded"
              aria-label={`Select ${m.name}`}
            />
            <Link href={`/memories/${m.id}`} className="flex-1 min-w-0">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="font-medium truncate">{m.name}</h3>
                  <p className="text-sm text-gray-500 dark:text-gray-400 line-clamp-2 mt-1">
                    {m.content.slice(0, 200)}
                  </p>
                </div>
                {m.stale && (
                  <span title="Stale memory"><AlertTriangle className="h-4 w-4 shrink-0 text-amber-500" /></span>
                )}
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Badge variant="secondary">{m.type}</Badge>
                <Badge variant="outline">{m.source_repo}</Badge>
                <span className="text-xs text-gray-400">{m.agent}</span>
                {m.tags.slice(0, 4).map((t) => (
                  <Badge key={t} variant="default">{t}</Badge>
                ))}
                {m.tags.length > 4 && <span className="text-xs text-gray-400">+{m.tags.length - 4}</span>}
                <span className="ml-auto flex items-center gap-1 text-xs text-gray-400">
                  <Clock className="h-3 w-3" /> {timeAgo(m.updated_at)}
                </span>
              </div>
            </Link>
          </div>
        ))}
        {memories.length === 0 && (
          <div className="py-12 text-center text-gray-400">
            No memories found. {filters.q ? "Try a different search." : "Create one to get started."}
          </div>
        )}
      </div>
    </div>
  );
}
