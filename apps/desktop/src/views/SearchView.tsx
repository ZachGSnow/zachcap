import { Search } from "lucide-react";
import { useEffect, useState } from "react";

import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { usePoll } from "@/hooks/usePoll";
import { api, type SearchHit, type Settings } from "@/lib/engine";
import { clock, personColor } from "@/lib/format";
import { cn } from "@/lib/utils";

/** The engine marks matches with [brackets]; show them highlighted. */
function Snippet({ text }: { text: string }) {
  const parts = text.split(/(\[[^\]]*\])/g);
  return (
    <>
      {parts.map((part, index) =>
        part.startsWith("[") && part.endsWith("]") ? (
          <mark key={index} className="rounded bg-amber-300/50 px-0.5 text-foreground dark:bg-amber-500/30">
            {part.slice(1, -1)}
          </mark>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  );
}

export function SearchView({ onOpen }: { onOpen: (meetingId: number, at?: number) => void }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const { data: settings } = usePoll(() => api<Settings>("/api/settings"), 0);
  const me = settings?.me.name ?? "You";

  useEffect(() => {
    const words = query
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .map((word) => `"${word.replace(/"/g, "")}"*`);
    if (!words.length) {
      setHits(null);
      return;
    }
    // Answers for older text arriving late must not replace the results for what's typed now.
    let stale = false;
    const timer = window.setTimeout(() => {
      api<SearchHit[]>(`/api/search?q=${encodeURIComponent(words.join(" "))}`)
        .then((found) => !stale && setHits(found))
        .catch(() => !stale && setHits([]));
    }, 200);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [query]);

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className="mx-auto max-w-3xl px-8 py-8">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">Search</h1>
        <p className="mt-1 text-sm text-muted-foreground">Everything anyone said, in every meeting.</p>
        <div className="relative mt-6">
          <Search className="pointer-events-none absolute top-3 left-3 size-4 text-muted-foreground" />
          <Input
            autoFocus
            placeholder="firewall cutover, budget, Friday..."
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="h-10 pl-9 text-[15px]"
          />
        </div>
        {hits && (
          <p className="mt-4 text-xs text-muted-foreground">
            {hits.length === 0 ? "No matches." : `${hits.length}${hits.length >= 50 ? "+" : ""} match${hits.length === 1 ? "" : "es"}`}
          </p>
        )}
        <div className="mt-2 space-y-2">
          {hits?.map((hit, index) => (
            <button
              key={`${hit.meeting_id}-${hit.start}-${index}`}
              onClick={() => onOpen(hit.meeting_id, hit.start)}
              className="w-full rounded-xl border bg-card px-4 py-3 text-left transition-colors hover:bg-muted/50"
            >
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span className="font-medium text-foreground">{hit.title}</span>
                <span className="font-mono">{clock(hit.start)}</span>
                <span className="flex items-center gap-1">
                  <span className={cn("size-1.5 rounded-full", personColor(hit.speaker, hit.speaker === me).dot)} />
                  {hit.speaker}
                </span>
              </div>
              <p className="mt-1 text-sm">
                <Snippet text={hit.snippet} />
              </p>
            </button>
          ))}
        </div>
      </div>
    </ScrollArea>
  );
}
