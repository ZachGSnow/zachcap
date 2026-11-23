import type { MeetingStatus } from "./engine";

export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function minutes(seconds: number | null): string {
  if (!seconds) return "";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function hours(seconds: number): string {
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function day(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function dayGroup(iso: string): string {
  const date = new Date(iso);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  const days = Math.round((today.getTime() - d.getTime()) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "This week";
  if (days < 31) return "This month";
  return "Earlier";
}

export const STATUS_LABEL: Record<MeetingStatus, string> = {
  recorded: "Recorded",
  processing: "Processing",
  needs_names: "Needs names",
  ready: "Ready",
  failed: "Failed",
};

/** Accessible categorical colors; always shown next to the name, never alone. */
const PALETTE = [
  { dot: "bg-sky-500", soft: "bg-sky-500/15", text: "text-sky-700 dark:text-sky-300" },
  { dot: "bg-amber-500", soft: "bg-amber-500/15", text: "text-amber-700 dark:text-amber-300" },
  { dot: "bg-emerald-500", soft: "bg-emerald-500/15", text: "text-emerald-700 dark:text-emerald-300" },
  { dot: "bg-rose-500", soft: "bg-rose-500/15", text: "text-rose-700 dark:text-rose-300" },
  { dot: "bg-violet-500", soft: "bg-violet-500/15", text: "text-violet-700 dark:text-violet-300" },
  { dot: "bg-orange-500", soft: "bg-orange-500/15", text: "text-orange-700 dark:text-orange-300" },
  { dot: "bg-teal-500", soft: "bg-teal-500/15", text: "text-teal-700 dark:text-teal-300" },
  { dot: "bg-fuchsia-500", soft: "bg-fuchsia-500/15", text: "text-fuchsia-700 dark:text-fuchsia-300" },
];
const ME = { dot: "bg-foreground", soft: "bg-foreground/10", text: "text-foreground" };
const NOBODY = { dot: "bg-muted-foreground/40", soft: "bg-muted", text: "text-muted-foreground" };

export type SpeakerColor = (typeof PALETTE)[number];

function nameSlot(name: string): number {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
  return Math.abs(hash) % PALETTE.length;
}

/** A person's color everywhere (meetings, actions, search, people); you are the dark one. */
export function personColor(name: string, isMe = false): SpeakerColor {
  return isMe ? ME : PALETTE[nameSlot(name)];
}

/**
 * Colors for the speakers of one meeting: each person keeps their usual color unless
 * someone earlier in the list already has it, so no two speakers in a view look alike.
 */
export function colorMap(names: string[], me: string): (name: string | null | undefined) => SpeakerColor {
  const map = new Map<string, SpeakerColor>();
  const used = new Set<number>();
  for (const name of names) {
    if (map.has(name)) continue;
    if (name === me) {
      map.set(name, ME);
      continue;
    }
    let slot = nameSlot(name);
    for (let step = 0; step < PALETTE.length && used.has(slot); step++) slot = (slot + 1) % PALETTE.length;
    used.add(slot);
    map.set(name, PALETTE[slot]);
  }
  return (name) => (name ? (map.get(name) ?? (name === me ? ME : personColor(name))) : NOBODY);
}

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
}

/** dBFS (-60..0) to 0..100 for level meters. */
export function levelPercent(dbfs: number): number {
  return Math.max(0, Math.min(100, ((dbfs + 60) / 60) * 100));
}

export function toSeconds(stamp: string | undefined | null): number | null {
  if (!stamp) return null;
  const parts = stamp.replace(/[[\]]/g, "").split(":").map(Number);
  if (parts.some(Number.isNaN)) return null;
  return parts.reduce((total, part) => total * 60 + part, 0);
}
