import { Loader2, Plus, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { usePoll } from "@/hooks/usePoll";
import { api } from "@/lib/engine";
import { reason } from "@/lib/feedback";
import { cn } from "@/lib/utils";
import {
  PARTS,
  host,
  sendsLine,
  statusLine,
  waitLine,
  type Before,
  type ZachCoreParts,
  type ZachCoreStatus,
} from "@/lib/zachcore";

/** What leaves this PC, said where it's turned on and where it's changed. */
export function WhatLeaves() {
  return (
    <div className="grid gap-2 text-xs leading-relaxed text-muted-foreground">
      <p>
        <span className="font-medium text-foreground">What goes:</span> text only, over https to your server. A meeting's title,
        time, client and ticket, who was there and how much each of you talked, and what the switches allow: the summary and your
        notes, the tasks, the facts, the transcript.
      </p>
      <p>
        <span className="font-medium text-foreground">What never leaves this PC:</span> audio, voice samples, snapshots, attached
        files and dictation. A private meeting never goes.
      </p>
      <p>
        <span className="font-medium text-foreground">Taking back:</span> a meeting made private, deleted or redacted after it went
        is taken back from ZachCore, also while sending is off. Turning sending off stops what goes and takes nothing back: Take
        back does that, on a meeting or for all of them.
      </p>
      <p>A summary written on this PC sends no facts and no quotes.</p>
    </div>
  );
}

function Part({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="grid gap-3">
      <h3 className="text-sm font-medium">{title}</h3>
      {children}
    </section>
  );
}

/** Clients none of whose meetings go: typed, or picked from the ones known. */
function Never({ never, onChange }: { never: string[]; onChange: (never: string[]) => void }) {
  const { data: known = [] } = usePoll(() => api<string[]>("/api/clients"), 0);
  const [typed, setTyped] = useState("");
  const add = () => {
    const name = typed.trim();
    if (name && !never.some((other) => other.toLowerCase() === name.toLowerCase())) onChange([...never, name]);
    setTyped("");
  };
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap gap-1.5">
        {never.map((name) => (
          <Badge key={name} variant="secondary" className="gap-1 pr-1">
            {name}
            <button
              type="button"
              aria-label={`Send ${name}'s meetings again`}
              className="rounded-sm p-0.5 hover:bg-muted"
              onClick={() => onChange(never.filter((other) => other !== name))}
            >
              <X className="size-3" />
            </button>
          </Badge>
        ))}
        {!never.length && <span className="text-xs text-muted-foreground">Every client's meetings go.</span>}
      </div>
      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          add();
        }}
      >
        <Input list="zachcore-clients" placeholder="A client none of whose meetings go" value={typed} onChange={(event) => setTyped(event.target.value)} className="h-8 text-foreground" />
        <datalist id="zachcore-clients">
          {known.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
        <Button type="submit" size="sm" variant="outline" disabled={!typed.trim()}>
          <Plus className="size-3.5" />
          Add
        </Button>
      </form>
    </div>
  );
}

type Tested = { ok: boolean; status: number | null; error?: string };

/** Where meetings go, what goes, and what leaves this PC. The first time in three steps, ending
 * with turning it on; after that on one page. */
function Setup({ status, firstTime, onDone }: { status: ZachCoreStatus; firstTime: boolean; onDone: () => void }) {
  const [step, setStep] = useState(0);
  const [address, setAddress] = useState(status.address ?? "");
  const [space, setSpace] = useState(status.space || "work");
  const [key, setKey] = useState("");
  const [parts, setParts] = useState<ZachCoreParts>(status.parts);
  const [never, setNever] = useState(status.never);
  const [tested, setTested] = useState<Tested | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [before, setBefore] = useState<Before | null>(null);
  const [neverThere, setNeverThere] = useState<[string, number][]>([]);
  const connected = !!address.trim() && (!!key.trim() || (status.key && address.trim() === status.address));

  const test = async () => {
    setTesting(true);
    try {
      setTested(await api<Tested>("/api/zachcore/test", { method: "POST", json: { address, space, ...(key.trim() ? { key } : {}) } }));
    } catch (error) {
      setTested({ ok: false, status: null, error: reason(error) });
    } finally {
      setTesting(false);
    }
  };

  const save = async (choice?: "take_back" | "leave", on?: boolean) => {
    setSaving(true);
    try {
      if (!choice) {
        // Another server or space than the meetings went to: asked once, for all of them.
        const asked = await api<{ before: Before | null }>(
          `/api/zachcore/before?address=${encodeURIComponent(address.trim())}&space=${encodeURIComponent(space)}`,
        );
        if (asked.before) {
          setBefore(asked.before);
          return;
        }
      }
      const saved = await api<ZachCoreStatus>("/api/zachcore", {
        method: "PUT",
        json: { address, space, parts, never, ...(key.trim() ? { key } : {}), ...(choice ? { before: choice } : {}), ...(on === undefined ? {} : { on }) },
      });
      setKey("");
      setBefore(null);
      const there = Object.entries(saved.never_there ?? {}).filter(([, count]) => count > 0);
      toast.success(on ? "Sending to ZachCore is on" : "Saved");
      if (there.length) setNeverThere(there);
      else onDone();
    } catch (error) {
      toast.error("Couldn't save", { description: reason(error) });
    } finally {
      setSaving(false);
    }
  };

  const takeBackClient = async (client: string, takeBack: boolean) => {
    if (takeBack) {
      try {
        await api("/api/zachcore/take-back", { method: "POST", json: { client } });
        toast.success(`${client}'s meetings are taken back from ZachCore`);
      } catch (error) {
        toast.error("Couldn't take them back", { description: reason(error) });
      }
    }
    const left = neverThere.filter(([name]) => name !== client);
    setNeverThere(left);
    if (!left.length) onDone();
  };

  const connect = (
    <Part title="Connect" key="connect">
      <label className="grid gap-1 text-xs text-muted-foreground">
        The server's address
        <Input value={address} onChange={(event) => setAddress(event.target.value)} placeholder="https://notes.example.com" className="h-8 text-foreground" />
      </label>
      <label className="grid gap-1 text-xs text-muted-foreground">
        <span className="flex items-center gap-2">
          The key ZachCore made for ZachCap (its Sources page)
          {status.key && address.trim() === status.address && (
            <Badge variant="secondary" className="ml-auto">
              Saved
            </Badge>
          )}
        </span>
        <Input
          type="password"
          autoComplete="off"
          value={key}
          onChange={(event) => setKey(event.target.value)}
          placeholder={status.key && address.trim() === status.address ? "Saved in Windows Credential Manager; paste another to replace it" : "Paste the key"}
          className="h-8 text-foreground"
        />
      </label>
      <label className="grid gap-1 text-xs text-muted-foreground">
        The space it goes to
        <select
          value={space}
          onChange={(event) => setSpace(event.target.value)}
          className="h-8 rounded-md border border-input bg-transparent px-2 text-sm text-foreground"
        >
          <option value="work">Work</option>
          <option value="home">Home</option>
        </select>
      </label>
      <div className="flex items-center gap-2">
        <Button size="sm" variant="outline" disabled={!connected || testing} onClick={() => void test()}>
          {testing && <Loader2 className="size-3.5 animate-spin" />}
          Test the connection
        </Button>
        {tested && (
          <span className={cn("text-xs", tested.ok ? "text-emerald-600 dark:text-emerald-400" : "text-destructive")}>
            {tested.ok ? `Connected: ZachCore takes this key for ${space}.` : tested.error}
          </span>
        )}
      </div>
    </Part>
  );
  const whatGoes = (
    <Part title="What goes" key="what">
      <div className="divide-y rounded-lg border">
        {PARTS.map(({ part, label, help }) => (
          <div key={part} className="flex items-center gap-3 px-3 py-2.5">
            <div className="min-w-0 flex-1">
              <div className="text-sm">{label}</div>
              <p className="text-xs text-muted-foreground">{help}</p>
            </div>
            <Switch aria-label={label} checked={parts[part]} onCheckedChange={(on) => setParts({ ...parts, [part]: on })} />
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        The title, time, client, ticket, people and talk shares go with any of them. With all four off, nothing goes. Turning one
        off takes nothing out of ZachCore.
      </p>
      <div className="grid gap-1.5">
        <span className="text-xs font-medium">Never for these clients</span>
        <Never never={never} onChange={setNever} />
      </div>
    </Part>
  );
  const whatLeaves = (
    <Part title="What leaves this PC" key="leaves">
      <WhatLeaves />
    </Part>
  );
  const steps = [connect, whatGoes, whatLeaves];

  return (
    <>
      <Dialog open onOpenChange={(open) => !open && onDone()}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>{firstTime ? `Send meetings to ZachCore (${step + 1} of 3)` : "ZachCore"}</DialogTitle>
            <DialogDescription>
              {firstTime ? "Your own notes app and server. Nothing goes until you turn it on." : "Where meetings go, and what of them."}
            </DialogDescription>
          </DialogHeader>
          {firstTime ? steps[step] : <div className="grid gap-6">{steps}</div>}
          <DialogFooter>
            {firstTime && step > 0 && (
              <Button variant="outline" onClick={() => setStep(step - 1)}>
                Back
              </Button>
            )}
            {firstTime && step < 2 && (
              <Button disabled={step === 0 && !connected} onClick={() => setStep(step + 1)}>
                Next
              </Button>
            )}
            {firstTime && step === 2 && (
              <Button disabled={!connected || saving} onClick={() => void save(undefined, true)}>
                {saving && <Loader2 className="size-3.5 animate-spin" />}
                Turn on
              </Button>
            )}
            {!firstTime && (
              <Button disabled={!address.trim() || saving} onClick={() => void save()}>
                {saving && <Loader2 className="size-3.5 animate-spin" />}
                Save
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {before && (
        <Dialog open onOpenChange={(open) => !open && setBefore(null)}>
          <DialogContent className="sm:max-w-md">
            <DialogHeader>
              <DialogTitle>{before.count === 1 ? "1 meeting is" : `${before.count} meetings are`} somewhere else</DialogTitle>
              <DialogDescription>
                They went to {before.space} at {host(before.address)}. They go to {space} at {host(address)} as new meetings.
              </DialogDescription>
            </DialogHeader>
            <p className="text-xs text-muted-foreground">
              Taken back from there first, {host(before.address)} keeps only their time and title. Left there, it keeps them as they
              are. One made private, deleted or redacted, now or later, is taken back from there either way.
            </p>
            <DialogFooter>
              <Button variant="outline" disabled={saving} onClick={() => void save("leave", firstTime ? true : undefined)}>
                Leave them there
              </Button>
              <Button disabled={saving} onClick={() => void save("take_back", firstTime ? true : undefined)}>
                Take them back from there first
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {neverThere.length > 0 && (
        <Dialog open onOpenChange={(open) => !open && void takeBackClient(neverThere[0][0], false)}>
          <DialogContent className="sm:max-w-md">
            <DialogHeader>
              <DialogTitle>
                ZachCore has {neverThere[0][1] === 1 ? "1 meeting" : `${neverThere[0][1]} meetings`} of {neverThere[0][0]}
              </DialogTitle>
              <DialogDescription>Nothing more of them goes. Take back what it has?</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => void takeBackClient(neverThere[0][0], false)}>
                Leave them
              </Button>
              <Button onClick={() => void takeBackClient(neverThere[0][0], true)}>Take them back</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

/** Meetings from before sending was turned on: they go only when you say so. */
function Older({ onDone }: { onDone: () => void }) {
  const [since, setSince] = useState("");
  const { data } = usePoll(() => api<{ count: number }>(`/api/zachcore/older${since ? `?since=${since}` : ""}`), 0, since);
  const [busy, setBusy] = useState(false);
  const send = async () => {
    setBusy(true);
    try {
      const sent = await api<{ count: number }>("/api/zachcore/older", { method: "POST", json: since ? { since } : {} });
      toast.success(sent.count === 1 ? "1 meeting goes to ZachCore" : `${sent.count} meetings go to ZachCore`);
      onDone();
    } catch (error) {
      toast.error("Couldn't send them", { description: reason(error) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open onOpenChange={(open) => !open && onDone()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Send older meetings</DialogTitle>
          <DialogDescription>Meetings recorded before sending was turned on. Private ones and those of clients set to never don't go.</DialogDescription>
        </DialogHeader>
        <label className="grid gap-1 text-xs text-muted-foreground">
          Recorded from (empty: all of them)
          <Input type="date" value={since} onChange={(event) => setSince(event.target.value)} className="h-8 text-foreground" />
        </label>
        <DialogFooter>
          <Button disabled={!data?.count || busy} onClick={() => void send()}>
            {data === undefined ? "Counting..." : data.count === 1 ? "Send 1 meeting" : `Send ${data.count} meetings`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TakeEverythingBack({ status, onDone }: { status: ZachCoreStatus; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const take = async () => {
    setBusy(true);
    try {
      const taken = await api<{ count: number }>("/api/zachcore/take-back", { method: "POST", json: {} });
      toast.success(taken.count === 1 ? "1 meeting is taken back" : `${taken.count} meetings are taken back`);
      onDone();
    } catch (error) {
      toast.error("Couldn't take them back", { description: reason(error) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open onOpenChange={(open) => !open && onDone()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Take everything back from ZachCore?</DialogTitle>
          <DialogDescription>
            {status.there === 1 ? "1 meeting goes" : `${status.there} meetings go`} back out of ZachCore: it keeps their time and
            title, and what you changed there yourself. None of them goes again unless you choose Send now on it. New meetings still
            go while sending is on.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onDone}>
            Keep them there
          </Button>
          <Button variant="destructive" disabled={busy} onClick={() => void take()}>
            Take everything back
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The ZachCore section of Settings: one status line, and the rest behind Change... */
export function ZachCore() {
  const { data: status, refresh } = usePoll(() => api<ZachCoreStatus>("/api/zachcore"), 5000);
  const [open, setOpen] = useState<"setup" | "older" | "take" | null>(null);
  const done = () => {
    setOpen(null);
    void refresh();
  };
  if (!status) return <div className="px-4 py-3.5 text-sm text-muted-foreground">Looking...</div>;
  const set = !!status.address && status.key;
  const turn = async (on: boolean) => {
    try {
      await api("/api/zachcore", { method: "PUT", json: { on } });
      toast.success(on ? "Sending to ZachCore is on" : "Sending to ZachCore is off: nothing was taken back");
      await refresh();
    } catch (error) {
      toast.error("Couldn't change it", { description: reason(error) });
    }
  };
  return (
    <>
      <div className="grid gap-1 px-4 py-3.5">
        <div className="flex items-center gap-2">
          <span aria-hidden className={cn("size-2 shrink-0 rounded-full", status.on ? "bg-emerald-500" : "bg-muted-foreground/40")} />
          <span className="text-sm font-medium">{set ? statusLine(status) : "Send meetings to ZachCore"}</span>
          <div className="ml-auto flex items-center gap-2">
            {set && <Switch aria-label="Send meetings to ZachCore" checked={status.on} onCheckedChange={(on) => void turn(on)} />}
            <Button size="sm" variant="outline" onClick={() => setOpen("setup")}>
              {set ? "Change..." : "Set up..."}
            </Button>
          </div>
        </div>
        {set ? (
          <>
            <p className="text-xs text-muted-foreground">{waitLine(status)}</p>
            <p className="text-xs text-muted-foreground">{sendsLine(status)}</p>
          </>
        ) : (
          <p className="text-xs text-muted-foreground">
            Your meetings' summaries, tasks and facts in your own ZachCore, as meeting notes. Off until you turn it on; audio never
            leaves this PC.
          </p>
        )}
      </div>
      {set && (
        <div className="flex flex-wrap gap-2 px-4 py-3">
          <Button size="sm" variant="outline" disabled={!status.on} onClick={() => setOpen("older")}>
            Send older meetings...
          </Button>
          <Button size="sm" variant="outline" className="ml-auto" disabled={!status.there} onClick={() => setOpen("take")}>
            Take everything back...
          </Button>
        </div>
      )}
      {open === "setup" && <Setup status={status} firstTime={!set} onDone={done} />}
      {open === "older" && <Older onDone={done} />}
      {open === "take" && <TakeEverythingBack status={status} onDone={done} />}
    </>
  );
}
