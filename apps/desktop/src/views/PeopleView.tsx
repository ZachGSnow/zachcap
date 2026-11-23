import { AudioWaveform, MoreHorizontal, Pencil, Sparkles, Trash2, UserX, Users } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ClientField } from "@/components/Client";
import { NamePrompt } from "@/components/Transcript";
import { PersonAvatar } from "@/components/Visuals";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { usePoll } from "@/hooks/usePoll";
import { api, type Person } from "@/lib/engine";
import { day, minutes, personColor } from "@/lib/format";

export function PeopleView({ onBrief }: { onBrief: (name: string) => void }) {
  const { data: people, refresh } = usePoll(() => api<Person[]>("/api/people"), 0);
  const { data: known = [], refresh: refreshKnown } = usePoll(() => api<string[]>("/api/clients"), 0);
  const [renaming, setRenaming] = useState<Person | null>(null);
  const [removing, setRemoving] = useState<Person | null>(null);
  const top = Math.max(...(people ?? []).map((p) => p.talk_seconds), 1);

  const act = async (label: string, action: () => Promise<unknown>) => {
    try {
      await action();
      toast.success(label);
      await Promise.all([refresh(), refreshKnown()]); // a rename or removal can change the clients
    } catch (error) {
      toast.error("That didn't work", { description: String(error) });
    }
  };

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className="mx-auto max-w-4xl px-8 py-8">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">People</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Voices ZachCap knows. Each confirmed meeting adds a voice sample, which makes recognition more reliable.
          Samples never leave this computer.
        </p>

        {people && people.length <= 1 && (
          <div className="mt-12 flex flex-col items-center text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted">
              <Users className="size-5" />
            </div>
            <p className="mt-4 font-medium">Nobody yet</p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              Name a speaker in any meeting and they appear here, with their voice remembered for next time.
            </p>
          </div>
        )}

        <div className="mt-6 grid gap-3 sm:grid-cols-2">
          {people?.map((person) => {
            const isMe = Boolean(person.is_me);
            return (
              <div key={person.id} className="rounded-xl border bg-card p-4">
                <div className="flex items-start gap-3">
                  <PersonAvatar name={person.name} color={personColor(person.name, isMe)} size="lg" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate font-medium">{person.name}</span>
                      {isMe && person.name !== "You" && <Badge variant="secondary">You</Badge>}
                    </div>
                    <div className="mt-0.5 text-xs text-muted-foreground">
                      {person.meetings} meeting{person.meetings === 1 ? "" : "s"}
                      {person.last_seen ? ` \u00b7 last ${day(person.last_seen)}` : ""}
                    </div>
                    {!isMe && (
                      <div className="-ml-1.5 mt-1 text-muted-foreground">
                        <ClientField
                          value={person.org}
                          known={known}
                          hint="The client they work for: their meetings are that client's"
                          onSave={(client) =>
                            void act(client ? `${person.name} works for ${client}` : `${person.name} has no client now`, async () => {
                              await api(`/api/people/${person.id}/client`, { method: "PUT", json: { client } });
                              await refreshKnown();
                            })
                          }
                        />
                      </div>
                    )}
                  </div>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button size="icon" variant="ghost" className="size-7" aria-label="More">
                        <MoreHorizontal className="size-4" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-48">
                      {!isMe && person.meetings > 0 && (
                        <>
                          <DropdownMenuItem onSelect={() => onBrief(person.name)}>
                            <Sparkles className="size-4" />
                            Brief me
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                        </>
                      )}
                      <DropdownMenuItem onSelect={() => setRenaming(person)}>
                        <Pencil className="size-4" />
                        Rename
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        disabled={!person.samples}
                        onSelect={() =>
                          void act(`Forgot ${person.name}'s voice`, () =>
                            api(`/api/people/${person.id}/voice`, { method: "DELETE" }),
                          )
                        }
                      >
                        <UserX className="size-4" />
                        Forget voice
                      </DropdownMenuItem>
                      {!isMe && (
                        <>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem variant="destructive" onSelect={() => setRemoving(person)}>
                            <Trash2 className="size-4" />
                            Remove person
                          </DropdownMenuItem>
                        </>
                      )}
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
                <div className="mt-4">
                  <div className="mb-1 flex justify-between text-xs text-muted-foreground">
                    <span>Talk time</span>
                    <span className="tabular-nums">{minutes(person.talk_seconds) || "0 s"}</span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <div
                      className={`h-full rounded-full ${personColor(person.name, isMe).dot}`}
                      style={{ width: `${(person.talk_seconds / top) * 100}%` }}
                    />
                  </div>
                </div>
                <div className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
                  <AudioWaveform className="size-3.5" />
                  {person.samples
                    ? `${person.samples} voice sample${person.samples === 1 ? "" : "s"}`
                    : "Not recognized by voice yet"}
                </div>
              </div>
            );
          })}
        </div>
      </div>
      <NamePrompt
        open={renaming !== null}
        title={renaming?.is_me ? "Your name" : `Rename ${renaming?.name ?? ""}`}
        description="If the name belongs to someone else already, the two are merged."
        initial={renaming?.name ?? ""}
        people={people}
        onCancel={() => setRenaming(null)}
        onSubmit={(name) => {
          const person = renaming;
          setRenaming(null);
          if (person) void act(`Renamed to ${name}`, () => api(`/api/people/${person.id}`, { method: "PATCH", json: { name } }));
        }}
      />
      <Dialog open={removing !== null} onOpenChange={(open) => !open && setRemoving(null)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Remove {removing?.name}?</DialogTitle>
            <DialogDescription>
              Their voice samples are deleted and their lines in past meetings go back to an unnamed speaker. This
              can't be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRemoving(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                const person = removing;
                setRemoving(null);
                if (person) void act(`Removed ${person.name}`, () => api(`/api/people/${person.id}`, { method: "DELETE" }));
              }}
            >
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </ScrollArea>
  );
}
