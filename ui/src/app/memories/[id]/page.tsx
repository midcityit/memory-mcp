import { getMemory } from "@/lib/api";
import { MemoryDetail } from "./memory-detail";

export default async function MemoryPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const memory = await getMemory(id);
  return <MemoryDetail memory={memory} />;
}
