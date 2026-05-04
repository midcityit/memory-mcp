"use client";

import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { createMemoryAction } from "@/app/actions";
import { ArrowLeft } from "lucide-react";

const TYPES = ["architecture", "reference", "runbook", "note", "decision", "project", "project_state"];

export default function NewMemoryPage() {
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div className="flex items-center gap-3">
        <Link href="/memories">
          <Button variant="ghost" size="icon"><ArrowLeft className="h-4 w-4" /></Button>
        </Link>
        <h1 className="text-xl font-bold">New Memory</h1>
      </div>

      <form action={createMemoryAction} className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="type" className="block text-sm font-medium mb-1">Type</label>
            <Select name="type" id="type" required>
              {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </Select>
          </div>
          <div>
            <label htmlFor="name" className="block text-sm font-medium mb-1">Name</label>
            <Input name="name" id="name" required placeholder="Memory name" />
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="source_repo" className="block text-sm font-medium mb-1">Source Repo</label>
            <Input name="source_repo" id="source_repo" placeholder="e.g. global, memory-mcp" />
          </div>
          <div>
            <label htmlFor="agent" className="block text-sm font-medium mb-1">Agent</label>
            <Input name="agent" id="agent" placeholder="e.g. claude-code, kiro-cli" />
          </div>
        </div>

        <div>
          <label htmlFor="tags" className="block text-sm font-medium mb-1">Tags (comma-separated)</label>
          <Input name="tags" id="tags" placeholder="tag1, tag2, tag3" />
        </div>

        <div>
          <label htmlFor="content" className="block text-sm font-medium mb-1">Content (Markdown)</label>
          <Textarea name="content" id="content" required placeholder="Memory content..." className="min-h-[300px] font-mono text-sm" />
        </div>

        <div className="flex gap-2">
          <Button type="submit">Create Memory</Button>
          <Link href="/memories"><Button variant="outline" type="button">Cancel</Button></Link>
        </div>
      </form>
    </div>
  );
}
