import { invoke } from "@tauri-apps/api/core";

import type { ShortcutAction } from "@/lib/shortcuts";

/** Connection to the local Python engine (127.0.0.1 only, token-protected). */
export type EngineInfo = { port: number; token: string };

export type MeetingStatus = "recording" | "recorded" | "processing" | "needs_names" | "ready" | "failed";

export type Meeting = {
  id: number;
  title: string;
  started_at: string;
  duration: number | null;
  folder: string;
  status: MeetingStatus;
  progress: string | null;
  participants: string[];
  /** Lines by others that say your name. */
  mentions?: number;
  template?: string | null;
  /** Set when the recording was deleted by the "keep recordings" setting. */
  audio_removed_at?: string | null;
  /** The service ticket it was about, if any. */
  ticket?: string | null;
  /** Its client: set on it, or (inferred) the one its people all work for. */
  client?: string | null;
  client_inferred?: boolean;
  /** Set on it by you: with no client, that's "no client". */
  client_set?: boolean;
};

/** A ticket number found in a meeting, and where: offered to pick as its ticket. */
export type TicketFound = {
  ticket: string;
  where: ("title" | "notes" | "snapshots" | "transcript")[];
  count: number;
};

export type SpeakerStatus = "auto" | "suggested" | "confirmed" | "unknown" | "minor";

export type Speaker = {
  id: number;
  track: "mic" | "system";
  label: string;
  person_id: number | null;
  person_name: string | null;
  display_name: string;
  status: SpeakerStatus;
  match_score: number | null;
  speech_seconds: number | null;
  sample_start: number | null;
  sample_end: number | null;
};

export type Segment = {
  id: number;
  meeting_speaker_id: number | null;
  track: "mic" | "system";
  start: number;
  end: number;
  text: string;
  speaker: string;
  speaker_status: SpeakerStatus | null;
  /** 1 once you fixed the line's words. */
  edited?: number;
};

export type ActionKind = "commitment" | "action";

export type SummaryContent = {
  title: string;
  overview: string;
  decisions: string[];
  action_items: { owner: string; task: string; due: string | null; kind?: ActionKind; said_at?: string }[];
  who_said_what?: { speaker: string; points: string[] }[];
  open_questions: string[];
  topics: { title: string; start: string }[];
  template?: string;
};

export type Summary = {
  model: string;
  content: SummaryContent;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  created_at: string;
};

export type ActionItem = {
  id: number;
  meeting_id: number;
  owner: string;
  task: string;
  due: string | null;
  said_at: number | null;
  kind: ActionKind;
  status: "open" | "done";
  meeting_title: string;
  meeting_started_at: string;
};

export type TalkTime = { speaker: string; person_id: number | null; seconds: number; turns: number };

/** A line by someone else that says your name (or a name you answer to). `spans` are where
 * in the line's text the name was said, as [start, end) offsets (the engine's own rule). */
export type Mention = {
  segment_id: number;
  at: number;
  speaker: string;
  snippet: string;
  names: string[];
  spans: [number, number][];
};

/** A topic said in a meeting, with the lines (``spans`` mark what was counted). */
export type TopicFound = {
  topic: string;
  count: number;
  lines: { segment_id: number; at: number; speaker: string; snippet: string; spans: [number, number][] }[];
};

/** A topic across meetings, newest first. */
export type TopicOverview = {
  topic: string;
  words: string[];
  count: number;
  meetings: { id: number; title: string; started_at: string; client: string | null; count: number }[];
};

export type MeetingDetail = {
  meeting: Meeting;
  speakers: Speaker[];
  segments: Segment[];
  summary: Summary | null;
  notes: Note[];
  insights: Insights;
  talk_time: TalkTime[];
  action_items: ActionItem[];
  me: string;
  mentions: Mention[];
  /** Ticket numbers found in it, best first, other than its own. */
  ticket_suggestions: TicketFound[];
  /** The topics said in it, most said first. */
  topics?: TopicFound[];
  /** The clients of the people in it, and every client named so far. */
  clients_heard: string[];
  clients_known: string[];
};

export type Person = {
  id: number;
  name: string;
  is_me: number;
  samples: number;
  meetings: number;
  talk_seconds: number;
  last_seen: string | null;
  /** The client they work for. */
  org: string | null;
};

export type Stats = {
  meetings: number;
  seconds: number;
  week: number;
  week_seconds: number;
  open_actions: number;
  people: number;
  by_day: { day: string; seconds: number }[];
};

export type SearchHit = {
  meeting_id: number;
  title: string;
  start: number;
  speaker: string;
  snippet: string;
};

export type RecordState =
  | { recording: false }
  | {
      recording: true;
      title: string | null;
      elapsed: number;
      levels: { mic: number; system: number };
      devices?: { mic: string; system: string };
      errors?: string[];
      paused?: boolean;
      notes?: number;
      /** Things to check while it runs: a muted mic, no call audio, a nearly full disk. */
      warnings?: { key: string; text: string }[];
      /** Apps using the mic now, and how long ago the call app let go of it (the call ended). */
      call?: { apps: string[]; ended_for: number | null };
      /** Seconds since anyone was heard on either track. */
      quiet_for?: number;
    };

/** A snapshot's text is what Windows read in the picture of the call. */
export type Note = {
  id: number;
  meeting_id: number;
  at: number;
  kind: "note" | "bookmark" | "snapshot";
  text: string;
  created_at: string;
};

export type SpeakerInsight = {
  speaker: string;
  words: number;
  seconds: number;
  questions: number;
  interruptions: number;
  longest: number;
  longest_at: number | null;
  words_per_minute: number | null;
};

export type Insights = {
  speakers: SpeakerInsight[];
  longest: { speaker: string; seconds: number; at: number } | null;
  questions: number;
};

export type KeyStatus = Record<"anthropic" | "huggingface", string>;

export type Settings = {
  summary_model: string;
  summary_models: { id: string; label: string; input_usd: number; output_usd: number }[];
  language: string;
  languages: { id: string; label: string }[];
  mic_device: string;
  output_device: string;
  vocabulary: string;
  summary_template: string;
  export_folder: string;
  /** Recordings saved here import themselves; "" when off. */
  watch_folder: string;
  /** Files there that couldn't be imported, newest first. `retrying`: it couldn't be read
   * (in use, or the network share was away), and is tried again on its own. */
  watch_folder_failed: { name: string; error: string; retrying: boolean }[];
  /** Other names you answer to, comma-separated (for "said your name"). */
  also_answer_to: string;
  keep_audio_days: number;
  mask_before_claude: boolean;
  /** Count down and stop when the call ends, or after ten quiet minutes. */
  stop_when_call_ends: boolean;
  /** Keep a daily and weekly work log in the export folder too. */
  export_work_log: boolean;
  /** When a sound check last heard both sides (null: never). */
  soundcheck_passed: string | null;
  /** Hold the dictation keys to dictate into any app. */
  dictation: boolean;
  /** "cue: the text to type", one per line: saying just a cue types its text. */
  dictation_snippets: string;
  /** How ticket numbers look ("#######, INC-####": # a digit, ? a letter); "" when off. */
  ticket_patterns: string;
  /** "Topic: word, word", one per line. */
  topics: string;
  /** The keys for each global shortcut: picked in Settings, or the defaults. */
  shortcuts: Record<ShortcutAction, string>;
  templates: { id: string; label: string }[];
  recipes: { id: string; label: string }[];
  me: { id: number; name: string };
};

/** A recent dictation, kept in memory by the engine (never on disk). */
export type RecentDictation = {
  id: number;
  text: string;
  at: string;
  /** "typed" where the cursor was, "copied" to the clipboard instead, or "kept" (neither). */
  delivered: "typed" | "copied" | "kept";
};

export type RecipeResult = { title: string; text: string; model: string; cost_usd: number };

/** A kind of meeting (what a summary focuses on) or a one-click document. */
export type PromptKind = "template" | "recipe";

export type Prompt = {
  id: string;
  kind: PromptKind;
  label: string;
  instructions: string;
  /** Ships with ZachCap: can be edited and reset, not deleted. */
  builtin: boolean;
  /** A built-in that differs from how it ships. */
  edited: boolean;
};

export type PromptLibrary = {
  templates: Prompt[];
  recipes: Prompt[];
  limits: { label: number; instructions: number };
};

export type Devices = {
  inputs: { name: string; default: boolean }[];
  outputs: { name: string; default: boolean }[];
};

let info: EngineInfo | null = null;

export const isTauri = () => "__TAURI_INTERNALS__" in window;

export async function connect(): Promise<EngineInfo> {
  if (info) return info;
  if (isTauri()) {
    info = await invoke<EngineInfo>("engine_info");
  } else {
    // Browser development: start the engine yourself with a fixed port and token.
    info = {
      port: Number(import.meta.env.VITE_ZACHCAP_PORT ?? 8765),
      token: import.meta.env.VITE_ZACHCAP_TOKEN ?? "dev",
    };
  }
  return info;
}

function base(): string {
  if (!info) throw new Error("Engine not connected");
  return `http://127.0.0.1:${info.port}`;
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, headers, ...rest } = init;
  const response = await fetch(base() + path, {
    ...rest,
    headers: {
      Authorization: `Bearer ${info?.token}`,
      ...(json === undefined ? {} : { "Content-Type": "application/json" }),
      ...headers,
    },
    body: json === undefined ? rest.body : JSON.stringify(json),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(detail?.detail ?? `${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

/** URL for <audio> elements, which can't send an Authorization header. */
export function mediaUrl(path: string): string {
  return `${base()}${path}${path.includes("?") ? "&" : "?"}token=${info?.token}`;
}

export async function waitForEngine(timeoutMs = 180_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      await api("/api/health");
      return;
    } catch (error) {
      const stopped = isTauri() ? await invoke<string | null>("engine_exited") : null;
      if (stopped) throw new Error(stopped);
      if (Date.now() > deadline) throw error;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
}

export type WorkLogItem = {
  id: number;
  owner: string;
  task: string;
  due: string | null;
  status: "open" | "done";
  kind: string;
  said_at: number | null;
  meeting_id?: number;
  meeting_title?: string;
};

export type WorkLogMeeting = {
  id: number;
  title: string;
  started_at: string;
  /** "09:00-09:37" */
  time: string;
  seconds: number;
  /** Rounded up to the quarter hour. */
  hours: number;
  people: string[];
  overview: string;
  /** The overview's first sentence, for a time entry. */
  note: string;
  decisions: string[];
  items: WorkLogItem[];
  status: string;
  /** The service ticket it was about, if any. */
  ticket: string | null;
  client: string | null;
  /** Topics said in it, most said first. */
  topics?: { topic: string; count: number }[];
};

export type WorkLog = {
  period: "day" | "week";
  start: string;
  end: string;
  title: string;
  me: string;
  totals: { meetings: number; seconds: number; hours: number };
  days: { day: string; label: string; meetings: WorkLogMeeting[] }[];
  your_items: WorkLogItem[];
  waiting_on: WorkLogItem[];
  markdown: string;
  /** The one client it's for, if it was asked for one. */
  client: string | null;
};

export type SoundCheckSide = { device: string; peak_dbfs: number | null; heard: boolean };
export type SoundCheckResult = { mic: SoundCheckSide; system: SoundCheckSide; played: boolean; errors: string[] };

/** What a redaction did. Removing the summary, action items and the notes taken in the
 * stretch, and reporting an audio failure, came with the engine's privacy fix. */
export type RedactResult = {
  lines_removed: number;
  lines_redacted: number;
  has_summary: boolean;
  summary_removed?: boolean;
  items_removed?: number;
  notes_removed?: number;
  audio_error?: string | null;
  scrubbed?: boolean;
};
