"use client";

import { useState, useTransition } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Memory } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { updateMemoryAction, deleteMemoryAction } from "@/app/actions";
import { ArrowLeft, Pencil, Trash2, X, Plus, Save } from "lucide-react";

export function MemoryDetail({ memory }: { memory: Memory }) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(memory.content);
  const [tags, setTags] = useState(memory.tags);
  const [newTag, setNewTag] = useState("");
  const [isPending, startTransition] = useTransition();

  function addTag() {
    const t = newTag.trim();
    if (t && !tags.includes(t)) {
      setTags([...tags, t]);
      setNewTag("");
    }
  }

  function removeTag(tag: string) {
    setTags(tags.filter((t) => t !== tag));
  }

  function handleSave() {
    const fd = new FormData();
    fd.set("content", content);
    fd.set("tags", tags.join(","));
    startTransition(() => updateMemoryAction(memory.id, fd));
  }

  function handleDelete() {
    if (!confirm(`Delete "${memory.name}"?`)) return;
    startTransition(() => deleteMemoryAction(memory.id));
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <Link href="/memories">
            <Button variant="ghost" size="icon"><ArrowLeft className="h-4 w-4" /></Button>
          </Link>
          <div>
            <h1 className="text-xl font-bold">{memory.name}</h1>
            <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
              <Badge variant="secondary">{memory.type}</Badge>
              <Badge variant="outline">{memory.source_repo}</Badge>
              <span>{memory.agent}</span>
              {memory.stale && <Badge variant="destructive">stale</Badge>}
            </div>
          </div>
        </div>
        <div className="flex gap-2">
          {editing ? (
            <>
              <Button onClick={handleSave} disabled={isPending}><Save className="h-4 w-4" /> Save</Button>
              <Button variant="outline" onClick={() => { setEditing(false); setContent(memory.content); setTags(memory.tags); }}>Cancel</Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={() => setEditing(true)}><Pencil className="h-4 w-4" /> Edit</Button>
              <Button variant="destructive" onClick={handleDelete} disabled={isPending}><Trash2 className="h-4 w-4" /> Delete</Button>
            </>
          )}
        </div>
      </div>

      {/* Tags */}
      <div className="flex flex-wrap items-center gap-2">
        {(editing ? tags : memory.tags).map((tag) => (
          <Badge key={tag} variant="default" className="gap-1">
            {tag}
            {editing && (
              <button onClick={() => removeTag(tag)} className="ml-1 hover:text-red-500" aria-label={`Remove tag ${tag}`}>
                <X className="h-3 w-3" />
              </button>
            )}
          </Badge>
        ))}
        {editing && (
          <form
            onSubmit={(e) => { e.preventDefault(); addTag(); }}
            className="flex items-center gap-1"
          >
            <Input
              value={newTag}
              onChange={(e) => setNewTag(e.target.value)}
              placeholder="Add tag..."
              className="h-7 w-28 text-xs"
            />
            <Button type="submit" variant="ghost" size="sm"><Plus className="h-3 w-3" /></Button>
          </form>
        )}
      </div>

      {/* Content */}
      {editing ? (
        <Textarea value={content} onChange={(e) => setContent(e.target.value)} className="min-h-[400px] font-mono text-sm" />
      ) : (
        <div className="prose prose-sm dark:prose-invert max-w-none rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-6">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{memory.content}</ReactMarkdown>
        </div>
      )}

      {/* Metadata */}
      <div className="grid grid-cols-2 gap-4 text-sm text-gray-500 dark:text-gray-400 sm:grid-cols-4">
        <div><span className="block text-xs uppercase tracking-wider">ID</span><code className="text-xs">{memory.id}</code></div>
        <div><span className="block text-xs uppercase tracking-wider">Created</span>{new Date(memory.created_at).toLocaleString()}</div>
        <div><span className="block text-xs uppercase tracking-wider">Updated</span>{new Date(memory.updated_at).toLocaleString()}</div>
        <div><span className="block text-xs uppercase tracking-wider">Agent</span>{memory.agent}</div>
      </div>
    </div>
  );
}
