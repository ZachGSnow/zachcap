import { invoke } from "@tauri-apps/api/core";
import { disable, enable, isEnabled } from "@tauri-apps/plugin-autostart";
import {
  Copy,
  FolderOpen,
  KeyRound,
  Keyboard,
  MessageSquareText,
  Mic,
  Palette,
  Plug,
  Power,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Speaker,
  TriangleAlert,
  UserRound,
} from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { PromptList } from "@/components/PromptLibrary";
import { SETTINGS_CHANGED } from "@/components/Recording";
import { KeysButton } from "@/components/Shortcuts";
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
import { ScrollArea } from "@/components/ui/scroll-area";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { SoundCheck } from "@/components/SoundCheck";
import { usePoll } from "@/hooks/usePoll";
import { useReturnFocus } from "@/hooks/useReturnFocus";
import { api, isTauri, type Devices, type KeyStatus, type PromptLibrary, type Settings } from "@/lib/engine";
import { attempt, copyText, reason } from "@/lib/feedback";
import {
  DEFAULT_KEYS,
  setKeys,
  showKeys,
  useKeys,
  type ShortcutAction,
  type ShortcutStatus,
} from "@/lib/shortcuts";
import { getTheme, setTheme, type Theme } from "@/lib/theme";

const KEYS = [
  {
    name: "huggingface",
    title: "Hugging Face token",
    help: "Downloads the speaker-separation model once. Accept the model's terms on huggingface.co first.",
  },
  {
    name: "anthropic",
    title: "Anthropic API key",
    help: "Used for summaries. Only transcript text is sent, never audio.",
  },
] as const;

type SettingsChange = Partial<{
  summary_model: string;
  language: string;
  mic_device: string;
  output_device: string;
  vocabulary: string;
  summary_template: string;
  export_folder: string;
  keep_audio_days: number;
  mask_before_claude: boolean;
  stop_when_call_ends: boolean;
  export_work_log: boolean;
  dictation: boolean;
  also_answer_to: string;
  watch_folder: string;
  /** With watch_folder: import the recordings already there too. */
  watch_existing: boolean;
}>;

/** A folder whose recordings import themselves. Choosing one asks once about the
 * recordings already there (only new ones, unless you say so). */
function WatchFolderRow({
  folder,
  failed,
  onSave,
  onRetried,
}: {
  folder: string;
  failed: Settings["watch_folder_failed"];
  onSave: (change: SettingsChange) => Promise<void>;
  /** After Retry: Settings reads the list again. */
  onRetried: () => Promise<void>;
}) {
  const [asking, setAsking] = useState<{ path: string; count: number } | null>(null);
  // Files that aren't recordings ZachCap can read wait for a change or Retry; files that
  // couldn't be opened (in use, a share offline) are tried on every look anyway.
  const broken = failed.filter((file) => !file.retrying);
  const busy = failed.filter((file) => file.retrying);

  const retry = () =>
    attempt("Couldn't try them again", async () => {
      await api("/api/watch-folder/retry", { method: "POST" });
      toast.success(broken.length === 1 ? "It's tried again within a minute" : "They're tried again within a minute");
      await onRetried();
    });
  const returnFocus = useReturnFocus();

  const choose = () =>
    attempt("Couldn't choose the folder", async () => {
      let path: string | null = null;
      if (isTauri()) {
        const { open } = await import("@tauri-apps/plugin-dialog");
        const picked = await open({ directory: true, title: "Watch this folder for recordings" });
        path = typeof picked === "string" ? picked : null;
      } else {
        path = window.prompt("Full path of the folder to watch");
      }
      if (!path) return;
      const { count } = await api<{ count: number }>(`/api/watch-folder/files?path=${encodeURIComponent(path)}`);
      if (count > 0) setAsking({ path, count });
      else await onSave({ watch_folder: path, watch_existing: false });
    });

  const answer = async (existing: boolean) => {
    if (!asking) return;
    setAsking(null);
    await onSave({ watch_folder: asking.path, watch_existing: existing });
  };

  return (
    <>
      <Row
        label="Watch folder"
        help="Recordings saved here (a softphone's or phone system's exports, Teams recordings you download) import themselves within a minute. ZachCap only reads them."
      >
        <code className="min-w-0 flex-1 truncate font-mono text-xs" title={folder || undefined}>
          {folder || "Off"}
        </code>
        <Button
          size="sm"
          variant="outline"
          onClick={(event) => {
            returnFocus.remember(event.currentTarget);
            void choose();
          }}
        >
          <FolderOpen className="size-3.5" />
          Choose
        </Button>
        {folder && (
          <Button size="sm" variant="ghost" onClick={() => void onSave({ watch_folder: "" })}>
            Turn off
          </Button>
        )}
      </Row>
      {broken.length > 0 && (
        <div className="flex items-start gap-2 px-4 py-2.5 text-xs text-amber-700 dark:text-amber-300">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
          <span className="min-w-0 flex-1">
            {broken.length === 1 ? "1 file" : `${broken.length} files`} couldn't be imported: {broken[0].name}
            {broken[0].error ? ` (${broken[0].error})` : ""}
            {broken.length > 1 ? " and others" : ""}. They're tried again if they change.
          </span>
          <Button size="sm" variant="outline" className="h-6 shrink-0 px-2 text-xs" onClick={() => void retry()}>
            <RefreshCw className="size-3" />
            Retry
          </Button>
        </div>
      )}
      {busy.length > 0 && (
        <p className="flex items-start gap-2 px-4 py-2.5 text-xs text-muted-foreground">
          <RefreshCw className="mt-0.5 size-3.5 shrink-0" />
          <span className="min-w-0">
            {busy.length === 1 ? busy[0].name : `${busy.length} files, such as ${busy[0].name},`} can't be opened yet
            {busy[0].error ? ` (${busy[0].error})` : ""}: open in another program, or the folder is offline. ZachCap
            keeps trying, and imports {busy.length === 1 ? "it" : "them"} once {busy.length === 1 ? "it" : "they"} can
            be read.
          </span>
        </p>
      )}
      <Dialog open={asking !== null} onOpenChange={(open) => !open && setAsking(null)}>
        <DialogContent className="sm:max-w-md" onCloseAutoFocus={returnFocus.onCloseAutoFocus}>
          <DialogHeader>
            <DialogTitle>Import the recordings already there?</DialogTitle>
            <DialogDescription>
              {asking?.count === 1 ? "There is 1 recording" : `There are ${asking?.count} recordings`} in{" "}
              {asking?.path}. ZachCap can import them now, or only the ones saved from now on.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => void answer(true)}>
              Import them too
            </Button>
            <Button autoFocus onClick={() => void answer(false)}>
              Only new recordings
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

const THEMES: { value: Theme; label: string }[] = [
  { value: "system", label: "Same as Windows" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

function ThemeRow() {
  const [theme, setChoice] = useState<Theme>(getTheme);
  return (
    <Row label="Theme" help="Light, dark, or whatever Windows is set to.">
      <Select
        label="Theme"
        value={theme}
        onChange={(value) => {
          setTheme(value as Theme);
          setChoice(value as Theme);
        }}
        options={THEMES}
      />
    </Row>
  );
}

const KEEP_AUDIO = [
  { value: "0", label: "Forever" },
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
];

/** Rough tokens for an hour-long meeting summary, for the cost hint. */
const HOUR_INPUT_TOKENS = 15_000;
const HOUR_OUTPUT_TOKENS = 1_500;

function Section({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground">
        {icon}
        {title}
      </h2>
      <div className="divide-y overflow-hidden rounded-xl border bg-card">{children}</div>
    </section>
  );
}

function Row({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-2 px-4 py-3.5 sm:flex-row sm:items-center sm:gap-6">
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium">{label}</div>
        {help && <p className="mt-0.5 text-xs text-muted-foreground">{help}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-2 sm:w-80">{children}</div>
    </div>
  );
}

const SHORTCUTS: { action: ShortcutAction; label: string; help: string }[] = [
  { action: "record", label: "Start or stop recording", help: "Works from anywhere, even with ZachCap in the tray." },
  {
    action: "bookmark",
    label: "Bookmark this moment",
    help: "While recording, from anywhere. Summaries make sure to cover it. Held only while recording, so other apps can use these keys the rest of the time.",
  },
  {
    action: "snapshot",
    label: "Snapshot the call",
    help: "While recording, from anywhere: a picture of the call window, and the text Windows reads in it (an error message, a hostname), saved with the meeting's notes. Held only while recording.",
  },
  {
    action: "dictate",
    label: "Dictate",
    help: "Hold in any app to type what you say, while Dictation is on (above). Held only then.",
  },
];

/** A global shortcut's keys, which can be changed, and a warning when another app already
 * has them (they never reach ZachCap then). */
function ShortcutRow({
  action,
  label,
  help,
  keys,
  status,
  onPick,
}: {
  action: ShortcutAction;
  label: string;
  help: string;
  keys: string;
  /** What the shell found for these keys (absent outside the app). */
  status?: ShortcutStatus;
  /** New keys, or "" for the usual ones. */
  onPick: (action: ShortcutAction, keys: string) => Promise<void>;
}) {
  const found = status?.keys === keys ? status.state : undefined;
  return (
    <>
      <Row label={label} help={help}>
        <KeysButton label={label} keys={keys} onPick={(picked) => void onPick(action, picked)} />
        {keys !== DEFAULT_KEYS[action] && (
          <Button size="sm" variant="ghost" onClick={() => void onPick(action, "")}>
            Reset
          </Button>
        )}
      </Row>
      {(found === "taken" || found === "invalid") && (
        <div className="flex items-start gap-2 px-4 py-2.5 text-xs text-amber-700 dark:text-amber-300">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
          <span className="min-w-0 flex-1">
            {found === "taken"
              ? `Another app on this PC already has ${showKeys(keys)}, so pressing them never reaches ZachCap. Click the keys to pick others.`
              : `Windows can't use ${showKeys(keys)} as a shortcut. Click the keys to pick others.`}
          </span>
        </div>
      )}
    </>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="h-8 w-full min-w-0 rounded-md border bg-background px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

function KeyRow({ name, title, help, status, onSaved }: { name: string; title: string; help: string; status?: string; onSaved: () => void }) {
  const [value, setValue] = useState("");
  const set = status && status !== "not set";
  const save = async () => {
    try {
      await api(`/api/keys/${name}`, { method: "PUT", json: { value } });
      setValue("");
      toast.success(`${title} saved to Windows Credential Manager`);
      onSaved();
    } catch (error) {
      toast.error("Couldn't save", { description: String(error) });
    }
  };
  return (
    <div className="px-4 py-3.5">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">{title}</span>
        <Badge variant={set ? "secondary" : "outline"} className="ml-auto">
          {set ? "Saved" : "Not set"}
        </Badge>
      </div>
      <p className="mt-0.5 text-xs text-muted-foreground">{help}</p>
      <form
        className="mt-2.5 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <Input
          type="password"
          autoComplete="off"
          placeholder={set ? "Replace the saved key" : "Paste the key"}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          className="h-8"
        />
        <Button size="sm" type="submit" disabled={!value.trim()}>
          Save
        </Button>
      </form>
    </div>
  );
}

/** Names and jargon Whisper should spell right, with what it mishears them as. */
function WordsToKnow({ saved, onSave }: { saved: string; onSave: (value: string) => Promise<void> }) {
  const [value, setValue] = useState(saved);
  useEffect(() => setValue(saved), [saved]);
  const changed = value.trim() !== saved.trim();
  return (
    <div className="px-4 py-3.5">
      <div className="text-sm font-medium">Words to know</div>
      <p className="mt-0.5 text-xs text-muted-foreground">
        Names, products and jargon to spell right, one per line. Add what Whisper mishears after a colon, like{" "}
        <code className="font-mono">Proxmox: prox mocks</code>. People you've named are included automatically. Applies to
        meetings processed from now on (or Reprocess).
      </p>
      <Textarea
        aria-label="Words to know"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder={"Contoso\nProxmox: prox mocks, proxmocks\nConnectWise"}
        className="mt-2.5 min-h-24 font-mono text-xs"
      />
      <div className="mt-2 flex justify-end">
        <Button size="sm" disabled={!changed} onClick={() => void onSave(value)}>
          Save words
        </Button>
      </div>
    </div>
  );
}

function StartWithWindows() {
  const [on, setOn] = useState<boolean>();
  useEffect(() => {
    isEnabled()
      .then(setOn)
      .catch(() => setOn(false));
  }, []);

  const change = async (next: boolean) => {
    try {
      await (next ? enable() : disable());
      setOn(await isEnabled());
      toast.success(next ? "ZachCap will start with Windows" : "ZachCap won't start with Windows");
    } catch (error) {
      toast.error("Couldn't change that", { description: String(error) });
    }
  };

  return (
    <Row label="Start with Windows" help="Opens in the tray when you sign in, so it can offer to record your calls.">
      <Switch
        className="ml-auto"
        aria-label="Start with Windows"
        checked={on ?? false}
        disabled={on === undefined}
        onCheckedChange={(next) => void change(next)}
      />
    </Row>
  );
}

export function SettingsView() {
  const { data: settings, refresh } = usePoll(() => api<Settings>("/api/settings"), 0);
  const { data: keys, refresh: refreshKeys } = usePoll(() => api<KeyStatus>("/api/keys"), 0);
  const { data: health } = usePoll(() => api<{ version: string; data: string }>("/api/health"), 0);
  const { data: devices, refresh: refreshDevices } = usePoll(() => api<Devices>("/api/devices"), 0);
  const { data: mcp } = usePoll(() => api<{ claude_code: string; claude_desktop: string }>("/api/mcp"), 0);
  const { data: library, refresh: refreshLibrary } = usePoll(() => api<PromptLibrary>("/api/prompts"), 0);
  // An edit renames choices in the usual-kind picker, and a deleted kind can't stay the usual one.
  const libraryChanged = () => void Promise.all([refreshLibrary(), refresh()]);
  const [name, setName] = useState("");
  useEffect(() => setName(settings?.me.name ?? ""), [settings?.me.name]);
  const [aliases, setAliases] = useState("");
  useEffect(() => setAliases(settings?.also_answer_to ?? ""), [settings?.also_answer_to]);

  const keysNow = useKeys();
  const [shortcutStatus, setShortcutStatus] = useState<ShortcutStatus[]>([]);
  useEffect(() => {
    if (isTauri()) void invoke<ShortcutStatus[]>("shortcut_status").then(setShortcutStatus, () => undefined);
  }, []);
  const pickKeys = useCallback(
    async (action: ShortcutAction, keys: string) => {
      try {
        const saved = await api<Settings>("/api/settings", { method: "PUT", json: { shortcuts: { [action]: keys } } });
        setKeys(saved.shortcuts);
        if (isTauri()) setShortcutStatus(await invoke<ShortcutStatus[]>("set_shortcuts", { keys: saved.shortcuts }));
        toast.success(keys ? `Now ${showKeys(saved.shortcuts[action])}` : `Back to ${showKeys(DEFAULT_KEYS[action])}`);
        await refresh();
      } catch (error) {
        toast.error("Couldn't change the keys", { description: reason(error) });
      }
    },
    [refresh],
  );

  const save = async (change: SettingsChange) => {
    try {
      await api("/api/settings", { method: "PUT", json: change });
      toast.success("Saved");
      // A recording in progress reads some settings (Stop when the call ends): now, not later.
      window.dispatchEvent(new Event(SETTINGS_CHANGED));
      await refresh();
    } catch (error) {
      toast.error("Couldn't save", { description: reason(error) });
    }
  };

  const saveName = async () => {
    if (!settings || !name.trim() || name.trim() === settings.me.name) return;
    const saved = await attempt("Couldn't save your name", () =>
      api(`/api/people/${settings.me.id}`, { method: "PATCH", json: { name: name.trim() } }),
    );
    if (saved) toast.success(`Your lines now show as ${name.trim()}`);
    await refresh();
  };

  const chooseExportFolder = () =>
    attempt("Couldn't choose the folder", async () => {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const folder = await open({ directory: true, title: "Export meetings to" });
      if (typeof folder === "string") await save({ export_folder: folder });
    });

  // Empty records every output (the engine's default); "default" is the Windows default only.
  // A saved device that isn't plugged in now is listed as such: otherwise the picker shows
  // its first choice, which isn't what's saved (and picking it wouldn't be a change).
  const missing = (saved: string | undefined, list: { name: string }[] | undefined, special: string[]) =>
    saved && list && !special.includes(saved) && !list.some((d) => d.name === saved) ? saved : null;

  const outputOptions = (list: { name: string; default: boolean }[] | undefined, saved: string | undefined) => {
    const windowsDefault = list?.find((d) => d.default)?.name;
    const gone = missing(saved, list, ["", "default"]);
    return [
      { value: "", label: "Every speaker and headset (follows the call)" },
      { value: "default", label: `Windows default only${windowsDefault ? ` (${windowsDefault})` : ""}` },
      ...(list ?? []).map((d) => ({ value: d.name, label: `${d.name} only` })),
      ...(gone ? [{ value: gone, label: `${gone}: not connected, so every speaker and headset is recorded` }] : []),
    ];
  };

  const deviceOptions = (list: { name: string; default: boolean }[] | undefined, saved: string | undefined) => {
    const gone = missing(saved, list, [""]);
    return [
      { value: "", label: `Windows default${list?.find((d) => d.default) ? ` (${list.find((d) => d.default)!.name})` : ""}` },
      ...(list ?? []).map((d) => ({ value: d.name, label: d.name })),
      ...(gone ? [{ value: gone, label: `${gone}: not connected, so the Windows default is used` }] : []),
    ];
  };

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className="mx-auto max-w-3xl px-8 py-8">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">Settings</h1>

        <Section icon={<UserRound className="size-3.5" />} title="You">
          <Row label="Your name" help="How your own lines appear in transcripts, summaries and action items.">
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && void saveName()}
              className="h-8"
            />
            <Button size="sm" variant="outline" onClick={() => void saveName()} disabled={!name.trim() || name === settings?.me.name}>
              Save
            </Button>
          </Row>
          <Row
            label="Also answer to"
            help="Other names people call you, separated by commas: a nickname, initials. Meetings mark where someone said your name."
          >
            <Input
              aria-label="Also answer to"
              placeholder="Nickname, initials"
              value={aliases}
              onChange={(event) => setAliases(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && void save({ also_answer_to: aliases })}
              className="h-8"
            />
            <Button
              size="sm"
              variant="outline"
              onClick={() => void save({ also_answer_to: aliases })}
              disabled={aliases === (settings?.also_answer_to ?? "")}
            >
              Save
            </Button>
          </Row>
        </Section>

        <Section icon={<Mic className="size-3.5" />} title="Recording">
          <Row label="Microphone" help="Your voice. Use the one Teams uses: a headset, or the laptop mic.">
            <Select
              label="Microphone"
              value={settings?.mic_device ?? ""}
              onChange={(value) => void save({ mic_device: value })}
              options={deviceOptions(devices?.inputs, settings?.mic_device)}
            />
          </Row>
          <Row
            label="Computer audio from"
            help="Everyone else on the call. By default ZachCap records every speaker and headset at once, so a call on a headset, or a headset plugged in mid-call, isn't missed. Pick one device (or a virtual cable) to record only that; while it isn't connected, every speaker and headset is recorded instead."
          >
            <Select
              label="Computer audio from"
              value={settings?.output_device ?? ""}
              onChange={(value) => void save({ output_device: value })}
              options={outputOptions(devices?.outputs, settings?.output_device)}
            />
          </Row>
          <Row
            label="Sound check"
            help="Records four seconds as a meeting would, with a short sound halfway; say a few words while it listens. Shows both sides are heard. Nothing is kept."
          >
            <SoundCheck onDone={() => void refresh()} />
          </Row>
          <Row label="Devices" help="Plugged in a headset? Refresh the lists.">
            <Button size="sm" variant="outline" onClick={() => void refreshDevices()}>
              <RefreshCw className="size-3.5" />
              Refresh devices
            </Button>
          </Row>
          <Row
            label="Stop when the call ends"
            help="When Teams or another call app lets go of your microphone and it goes quiet, or after ten quiet minutes in a room, ZachCap counts down and stops. Keep recording cancels it."
          >
            <Switch
              className="ml-auto"
              aria-label="Stop when the call ends"
              checked={settings?.stop_when_call_ends ?? true}
              onCheckedChange={(on) => void save({ stop_when_call_ends: on })}
            />
          </Row>
        </Section>

        <Section icon={<Speaker className="size-3.5" />} title="Transcription">
          <Row label="Language" help="Whisper runs on your GPU. Pick the meeting language, or let it detect.">
            <Select
              label="Language"
              value={settings?.language ?? "en"}
              onChange={(value) => void save({ language: value })}
              options={(settings?.languages ?? []).map((l) => ({ value: l.id, label: l.label }))}
            />
          </Row>
          <WordsToKnow saved={settings?.vocabulary ?? ""} onSave={(vocabulary) => save({ vocabulary })} />
        </Section>

        <Section icon={<Sparkles className="size-3.5" />} title="Summaries">
          <Row label="Claude model" help="Opus 5.5 caught more implicit commitments in testing. Cost is per hour-long meeting.">
            <Select
              label="Claude model"
              value={settings?.summary_model ?? ""}
              onChange={(value) => void save({ summary_model: value })}
              options={(settings?.summary_models ?? []).map((m) => ({
                value: m.id,
                label: `${m.label} (about $${((HOUR_INPUT_TOKENS * m.input_usd + HOUR_OUTPUT_TOKENS * m.output_usd) / 1e6).toFixed(2)})`,
              }))}
            />
          </Row>
          <Row
            label="Usual kind of meeting"
            help="What summaries focus on unless you pick another kind for a meeting: action items, a customer's asks, an incident's timeline..."
          >
            <Select
              label="Usual kind of meeting"
              value={settings?.summary_template ?? "general"}
              onChange={(value) => void save({ summary_template: value })}
              options={(settings?.templates ?? []).map((t) => ({ value: t.id, label: t.label }))}
            />
          </Row>
          {library && (
            <>
              <PromptList kind="template" items={library.templates} limits={library.limits} onChanged={libraryChanged} />
              <PromptList kind="recipe" items={library.recipes} limits={library.limits} onChanged={libraryChanged} />
            </>
          )}
        </Section>

        <Section icon={<Plug className="size-3.5" />} title="Use your meetings in Claude">
          <div className="px-4 py-3.5">
            <p className="text-xs leading-relaxed text-muted-foreground">
              Add ZachCap to Claude Code or the Claude desktop app and ask about your meetings there. It's a read-only MCP
              server that can search transcripts and read summaries, action items and notes; it can't change anything. What
              it looks up goes to that app and its model, like anything else you share with it.
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={!mcp}
                onClick={() => mcp && void copyText(mcp.claude_code, "Command copied: paste it in a terminal")}
              >
                <Copy className="size-3.5" />
                Copy command for Claude Code
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={!mcp}
                onClick={() =>
                  mcp && void copyText(mcp.claude_desktop, "Copied: add it to claude_desktop_config.json, then restart Claude")
                }
              >
                <Copy className="size-3.5" />
                Copy config for the Claude app
              </Button>
            </div>
          </div>
        </Section>

        <Section icon={<KeyRound className="size-3.5" />} title="API keys">
          {KEYS.map((key) => (
            <KeyRow key={key.name} {...key} status={keys?.[key.name]} onSaved={() => void refreshKeys()} />
          ))}
        </Section>

        {/* Release builds only: the shell registers autostart only there. */}
        {isTauri() && import.meta.env.PROD && (
          <Section icon={<Power className="size-3.5" />} title="Startup">
            <StartWithWindows />
          </Section>
        )}

        <Section icon={<Palette className="size-3.5" />} title="Appearance">
          <ThemeRow />
        </Section>

        <Section icon={<MessageSquareText className="size-3.5" />} title="Dictation">
          <Row
            label={`Hold ${keysNow.dictate} to dictate`}
            help="In any app: hold the keys, speak, let go, and what you said is typed where your cursor is, spelled with your Words to know. Transcribed on this computer (a small model, ready a few seconds after you turn this on); nothing is recorded or kept. If you've moved to another window by then, it's copied instead, ready to paste. During a recording, what you dictate is left out of the meeting (the call app still hears you: mute it first)."
          >
            <Switch
              className="ml-auto"
              aria-label={`Hold ${keysNow.dictate} to dictate`}
              checked={settings?.dictation ?? false}
              onCheckedChange={(on) => void save({ dictation: on })}
            />
          </Row>
          {settings?.dictation && (
            <Row label="Try it here" help={`Click in the box, hold ${keysNow.dictate} and say something.`}>
              <Textarea aria-label="Try dictation here" placeholder={`Hold ${keysNow.dictate} and speak`} className="min-h-16 text-sm" />
            </Row>
          )}
        </Section>

        <Section icon={<Keyboard className="size-3.5" />} title="Shortcuts">
          {SHORTCUTS.map((shortcut) => (
            <ShortcutRow
              key={shortcut.action}
              {...shortcut}
              keys={keysNow[shortcut.action]}
              status={shortcutStatus.find((status) => status.action === shortcut.action)}
              onPick={pickKeys}
            />
          ))}
          <Row label="Play or pause" help="On a meeting's page.">
            <kbd className="rounded-md border bg-muted px-2 py-1 font-mono text-xs">Space</kbd>
            <kbd className="rounded-md border bg-muted px-2 py-1 font-mono text-xs">K</kbd>
          </Row>
          <Row label="Back or forward 5 seconds" help="On a meeting's page.">
            <kbd className="rounded-md border bg-muted px-2 py-1 font-mono text-xs">Left</kbd>
            <kbd className="rounded-md border bg-muted px-2 py-1 font-mono text-xs">Right</kbd>
          </Row>
          <Row label="Back or forward 10 seconds" help="On a meeting's page.">
            <kbd className="rounded-md border bg-muted px-2 py-1 font-mono text-xs">J</kbd>
            <kbd className="rounded-md border bg-muted px-2 py-1 font-mono text-xs">L</kbd>
          </Row>
        </Section>

        <Section icon={<ShieldCheck className="size-3.5" />} title="Storage and privacy">
          <Row label="Data folder" help="Recordings, transcripts, the search index and voiceprints. Delete a meeting to remove everything learned from it.">
            <code className="min-w-0 flex-1 truncate font-mono text-xs" title={health?.data}>
              {health?.data ?? "..."}
            </code>
            {isTauri() && health?.data && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => void attempt("Couldn't open the folder", () => invoke("open_data_folder"))}
              >
                <FolderOpen className="size-3.5" />
                Open
              </Button>
            )}
          </Row>
          <Row
            label="Export folder"
            help="Also keep every meeting there as a Markdown file, updated as it changes: for Obsidian or a notes folder. Pick a folder that isn't synced to a work cloud if meetings are confidential."
          >
            <code className="min-w-0 flex-1 truncate font-mono text-xs" title={settings?.export_folder || undefined}>
              {settings?.export_folder || "Off"}
            </code>
            {isTauri() && (
              <Button size="sm" variant="outline" onClick={() => void chooseExportFolder()}>
                <FolderOpen className="size-3.5" />
                Choose
              </Button>
            )}
            {settings?.export_folder && (
              <Button size="sm" variant="ghost" onClick={() => void save({ export_folder: "" })}>
                Turn off
              </Button>
            )}
          </Row>
          {settings?.export_folder && (
            <Row
              label="Work logs there too"
              help="A daily and a weekly work log (meetings, action items, time entries) next to the meetings, kept up to date: Work log 2026-11-23.md, Work log 2026-W48.md."
            >
              <Switch
                className="ml-auto"
                aria-label="Keep work logs in the export folder"
                checked={settings.export_work_log}
                onCheckedChange={(on) => void save({ export_work_log: on })}
              />
            </Row>
          )}
          <WatchFolderRow
            folder={settings?.watch_folder ?? ""}
            failed={settings?.watch_folder_failed ?? []}
            onSave={save}
            onRetried={refresh}
          />
          <Row
            label="Keep recordings"
            help="After this, a meeting's audio and its snapshot pictures are deleted; its transcript, summary, notes, the text read in its snapshots and the voices it taught stay. It can't be played or reprocessed after."
          >
            <Select
              label="Keep recordings"
              value={String(settings?.keep_audio_days ?? 0)}
              onChange={(value) => void save({ keep_audio_days: Number(value) })}
              options={KEEP_AUDIO}
            />
          </Row>
          <Row
            label="Mask contact details for Claude"
            help="Emails, phone numbers and card numbers are replaced with [email], [phone] and [card number] in everything sent to Claude, including through the MCP server."
          >
            <Switch
              className="ml-auto"
              aria-label="Mask contact details for Claude"
              checked={settings?.mask_before_claude ?? false}
              onCheckedChange={(mask) => void save({ mask_before_claude: mask })}
            />
          </Row>
          <div className="px-4 py-3.5 text-xs leading-relaxed text-muted-foreground">
            Audio and voiceprints never leave this computer. Summaries send transcript text to Anthropic with your API
            key. Voiceprints are biometric data in some places, and recording laws differ: tell people you're recording.
          </div>
        </Section>

        <p className="mt-8 text-xs text-muted-foreground">ZachCap {health?.version ?? ""} {"\u00b7"} MIT license</p>
      </div>
    </ScrollArea>
  );
}
