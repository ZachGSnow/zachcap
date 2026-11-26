/** Sending to ZachCore, as Settings and a meeting show it: the engine says what (sender.py's
 * status and meeting_status), and these say it in words. */

export type ZachCoreWhy =
  | { kind: "refused"; status: number }
  | { kind: "no_server" }
  | { kind: "no_key"; address: string }
  | { kind: "off" }
  | { kind: "full"; until: number }
  | { kind: "paused"; until: number }
  | { kind: "no_answer"; until: number }
  | { kind: "refused_one"; count: number }
  | { kind: "elsewhere"; count: number }
  | { kind: "busy" }
  | { kind: "names" }
  | { kind: "quiet" }
  | { kind: "soon" };

export type ZachCoreParts = { summaries: boolean; tasks: boolean; facts: boolean; transcripts: boolean };

export type ZachCorePlace = { place: string; address: string; space: string };

export type ZachCoreStatus = {
  on: boolean;
  address: string | null;
  space: string;
  /** Whether a key is saved for the address (in Credential Manager): never the key. */
  key: boolean;
  parts: ZachCoreParts;
  /** Clients none of whose meetings go. */
  never: string[];
  /** Meetings whose message or transcript waits, and the first thing that holds them up. */
  waiting: number;
  why: ZachCoreWhy | null;
  taking_back: number;
  taking_back_why: ZachCoreWhy | null;
  /** Meetings ZachCore may have. */
  there: number;
  /** Of those, the ones on another server or space than now. */
  elsewhere: (ZachCorePlace & { count: number })[];
  last_sent: string | null;
  /** After a change: for each client just set to never, how many of its meetings ZachCore has. */
  never_there?: Record<string, number>;
};

export type MeetingZachCore = {
  there: boolean;
  place: ZachCorePlace | null;
  sent_at: string | null;
  waiting: boolean;
  taking_back: boolean;
  /** Its server's key is saved: what it has can be taken back. */
  can_take_back: boolean;
  taken: boolean;
  wanted: boolean;
  on: boolean;
  /** Why it never goes: "It is private", "Nothing of Contoso goes". */
  kept: string | null;
};

/** When a server or space changes and meetings went to the old one: what of them. */
export type Before = { place: string; address: string; space: string; count: number };

export const PARTS: { part: keyof ZachCoreParts; label: string; help: string }[] = [
  { part: "summaries", label: "Summaries", help: "The overview, your notes, decisions, open questions and topics." },
  { part: "tasks", label: "Tasks", help: "Action items: yours, who promised what, and when it's due." },
  { part: "facts", label: "Facts", help: "Lasting facts about the people and the client, each waiting for your yes in ZachCore." },
  { part: "transcripts", label: "Transcripts", help: "What was said, line by line: once the voices have names, or after a day." },
];

/** The server's name in an address: "notes.example.com", or "this PC" for 127.0.0.1. */
export function host(address: string | null | undefined): string {
  if (!address) return "";
  try {
    const url = new URL(address);
    return url.hostname === "127.0.0.1" ? `this PC${url.port ? `, port ${url.port}` : ""}` : url.host;
  } catch {
    return address;
  }
}

/** A time today as "2:14 PM", on another day as "Nov 24, 2:14 PM". Seconds since 1970, or ISO. */
export function at(when: number | string, now = new Date()): string {
  const date = typeof when === "number" ? new Date(when * 1000) : new Date(when);
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (date.toDateString() === now.toDateString()) return time;
  return `${date.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${time}`;
}

function meetings(count: number): string {
  return count === 1 ? "1 meeting" : `${count} meetings`;
}

/** "summaries, tasks and facts". */
export function listed(names: string[]): string {
  return names.length < 2 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** Why they wait, in a few words. */
export function whyText(why: ZachCoreWhy | null, now = new Date()): string | null {
  if (!why) return null;
  switch (why.kind) {
    case "refused":
      return why.status === 404 ? "ZachCore has no such space" : `ZachCore refused the key (${why.status})`;
    case "no_server":
      return "no server is set";
    case "no_key":
      return `no key is saved for ${host(why.address)}`;
    case "off":
      return "sending is off";
    case "full":
      return `ZachCore is out of writing until ${at(why.until, now)}`;
    case "paused":
      return "paused in ZachCore";
    case "no_answer":
      return `ZachCore didn't answer; next try ${at(why.until, now)}`;
    case "refused_one":
      return `ZachCore refused ${meetings(why.count)}; it goes again once changed`;
    case "elsewhere":
      return `${meetings(why.count)} went to another server or space before`;
    case "busy":
      return "they go once they are processed";
    case "names":
      return "transcripts wait for the voices' names, or a day";
    case "quiet":
      return "half a minute after the last change";
    case "soon":
      return "going now";
  }
}

/** The status line's first part: "On, work at notes.example.com". */
export function statusLine(status: ZachCoreStatus): string {
  if (!status.address) return "Not set up";
  return `${status.on ? "On" : "Off"}, ${status.space} at ${host(status.address)}`;
}

/** "Last sent 2:14 PM. 3 wait: paused in ZachCore." */
export function waitLine(status: ZachCoreStatus, now = new Date()): string {
  const said: string[] = [];
  if (status.last_sent) said.push(`Last sent ${at(status.last_sent, now)}.`);
  if (status.waiting) {
    const why = whyText(status.why, now);
    said.push(`${status.waiting} ${status.waiting === 1 ? "waits" : "wait"}${why ? `: ${why}` : ""}.`);
  }
  if (status.taking_back) {
    const why = whyText(status.taking_back_why, now);
    said.push(`${meetings(status.taking_back)} to be taken back${why ? `: ${why}` : ""}.`);
  }
  if (!status.waiting && !status.taking_back) said.push(status.there ? `${meetings(status.there)} in ZachCore.` : "Nothing sent yet.");
  return said.join(" ");
}

/** "Sends summaries, tasks and facts. Never Contoso." */
export function sendsLine(status: ZachCoreStatus): string {
  const on = PARTS.filter(({ part }) => status.parts[part]).map(({ label }) => label.toLowerCase());
  const sends = on.length ? `Sends ${listed(on)}.` : "Sends nothing: every switch is off.";
  return status.never.length ? `${sends} Never ${listed(status.never)}.` : sends;
}

/** What a meeting says of ZachCore, or null when there's nothing to say. */
export function meetingLine(meeting: MeetingZachCore | null | undefined, now = new Date()): string | null {
  if (!meeting) return null;
  if (meeting.taking_back) {
    return meeting.can_take_back
      ? "Being taken back from ZachCore"
      : `Still in ZachCore, and can't be taken back: no key is saved for ${host(meeting.place?.address)}`;
  }
  if (meeting.kept?.startsWith("Nothing of")) return `${meeting.kept} to ZachCore`;
  if (meeting.kept) return null; // private: the meeting says so already
  if (meeting.waiting) return meeting.there ? "Its changes wait to go to ZachCore" : "Waits to go to ZachCore";
  if (meeting.there && meeting.sent_at) return `Sent to ZachCore ${at(meeting.sent_at, now)}`;
  if (meeting.taken) return "Taken back from ZachCore";
  return null;
}

/** Whether a meeting's menu offers Send now and Take back. */
export function menuOf(meeting: MeetingZachCore | null | undefined): { send: boolean; takeBack: boolean } {
  if (!meeting) return { send: false, takeBack: false };
  return {
    send: meeting.on && !meeting.kept && !meeting.taking_back,
    takeBack: (meeting.there || meeting.waiting) && !meeting.taking_back,
  };
}
