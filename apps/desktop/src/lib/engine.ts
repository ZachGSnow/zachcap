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
    };

export type Note = { id: number; meeting_id: number; at: number; kind: "note" | "bookmark"; text: string };

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
  templates: { id: string; label: string }[];
  recipes: { id: string; label: string }[];
  me: { id: number; name: string };
};

export type RecipeResult = { title: string; text: string; model: string; cost_usd: number };

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
