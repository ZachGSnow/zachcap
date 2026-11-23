import { invoke } from "@tauri-apps/api/core";

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
  template?: string | null;
  /** Set when the recording was deleted by the "keep recordings" setting. */
  audio_removed_at?: string | null;
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
};

export type Person = {
  id: number;
  name: string;
  is_me: number;
  samples: number;
  meetings: number;
  talk_seconds: number;
  last_seen: string | null;
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

export type Note = { id: number; meeting_id: number; at: number; kind: "note" | "bookmark"; text: string };

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
  keep_audio_days: number;
  mask_before_claude: boolean;
  /** Count down and stop when the call ends, or after ten quiet minutes. */
  stop_when_call_ends: boolean;
  templates: { id: string; label: string }[];
  recipes: { id: string; label: string }[];
  me: { id: number; name: string };
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
