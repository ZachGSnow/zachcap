import { invoke } from "@tauri-apps/api/core";
import { disable, enable, isEnabled } from "@tauri-apps/plugin-autostart";
import {
  Copy,
  FolderOpen,
  KeyRound,
  Keyboard,
  Mic,
  Plug,
  Power,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Speaker,
  UserRound,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { usePoll } from "@/hooks/usePoll";
import { api, isTauri, type Devices, type KeyStatus, type Settings } from "@/lib/engine";
import { attempt, copyText } from "@/lib/feedback";

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
  const [name, setName] = useState("");
  useEffect(() => setName(settings?.me.name ?? ""), [settings?.me.name]);

  const save = async (
    change: Partial<
      Record<
        "summary_model" | "language" | "mic_device" | "output_device" | "vocabulary" | "summary_template" | "export_folder",
        string
      >
    >,
  ) => {
    try {
      await api("/api/settings", { method: "PUT", json: change });
      toast.success("Saved");
      await refresh();
    } catch (error) {
      toast.error("Couldn't save", { description: String(error) });
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

  const chooseExportFolder = async () => {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const folder = await open({ directory: true, title: "Export meetings to" });
    if (typeof folder === "string") await save({ export_folder: folder });
  };

  const deviceOptions = (list: { name: string; default: boolean }[] | undefined) => [
    { value: "", label: `Windows default${list?.find((d) => d.default) ? ` (${list.find((d) => d.default)!.name})` : ""}` },
    ...(list ?? []).map((d) => ({ value: d.name, label: d.name })),
  ];

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
        </Section>

        <Section icon={<Mic className="size-3.5" />} title="Recording">
          <Row label="Microphone" help="Your voice. Use the one Teams uses: a headset, or the laptop mic.">
            <Select
              label="Microphone"
              value={settings?.mic_device ?? ""}
              onChange={(value) => void save({ mic_device: value })}
              options={deviceOptions(devices?.inputs)}
            />
          </Row>
          <Row
            label="Computer audio from"
            help="Everyone else: ZachCap records what plays on this device. No virtual microphone needed; a virtual cable works too if you use one."
          >
            <Select
              label="Computer audio from"
              value={settings?.output_device ?? ""}
              onChange={(value) => void save({ output_device: value })}
              options={deviceOptions(devices?.outputs)}
            />
          </Row>
          <Row label="Devices" help="Plugged in a headset? Refresh the lists.">
            <Button size="sm" variant="outline" onClick={() => void refreshDevices()}>
              <RefreshCw className="size-3.5" />
              Refresh devices
            </Button>
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

        <Section icon={<Keyboard className="size-3.5" />} title="Shortcuts">
          <Row label="Start or stop recording" help="Works from anywhere, even with ZachCap in the tray.">
            <kbd className="rounded-md border bg-muted px-2 py-1 font-mono text-xs">Ctrl + Alt + R</kbd>
          </Row>
          <Row label="Bookmark this moment" help="While recording, from anywhere. Summaries make sure to cover it.">
            <kbd className="rounded-md border bg-muted px-2 py-1 font-mono text-xs">Ctrl + Alt + B</kbd>
          </Row>
          <Row label="Play or pause" help="On a meeting's page.">
            <kbd className="rounded-md border bg-muted px-2 py-1 font-mono text-xs">Space</kbd>
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
