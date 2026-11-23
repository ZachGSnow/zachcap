import { invoke } from "@tauri-apps/api/core";
import { FolderOpen, KeyRound, Keyboard, Mic, RefreshCw, ShieldCheck, Sparkles, Speaker, UserRound } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { usePoll } from "@/hooks/usePoll";
import { api, isTauri, type Devices, type KeyStatus, type Settings } from "@/lib/engine";

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
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select
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

export function SettingsView() {
  const { data: settings, refresh } = usePoll(() => api<Settings>("/api/settings"), 0);
  const { data: keys, refresh: refreshKeys } = usePoll(() => api<KeyStatus>("/api/keys"), 0);
  const { data: health } = usePoll(() => api<{ version: string; data: string }>("/api/health"), 0);
  const { data: devices, refresh: refreshDevices } = usePoll(() => api<Devices>("/api/devices"), 0);
  const [name, setName] = useState("");
  useEffect(() => setName(settings?.me.name ?? ""), [settings?.me.name]);

  const save = async (change: Partial<Record<"summary_model" | "language" | "mic_device" | "output_device", string>>) => {
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
    await api(`/api/people/${settings.me.id}`, { method: "PATCH", json: { name: name.trim() } });
    toast.success(`Your lines now show as ${name.trim()}`);
    await refresh();
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
              value={settings?.language ?? "en"}
              onChange={(value) => void save({ language: value })}
              options={(settings?.languages ?? []).map((l) => ({ value: l.id, label: l.label }))}
            />
          </Row>
        </Section>

        <Section icon={<Sparkles className="size-3.5" />} title="Summaries">
          <Row label="Claude model" help="Opus 5.5 caught more implicit commitments in testing. Cost is per hour-long meeting.">
            <Select
              value={settings?.summary_model ?? ""}
              onChange={(value) => void save({ summary_model: value })}
              options={(settings?.summary_models ?? []).map((m) => ({
                value: m.id,
                label: `${m.label} (about $${((HOUR_INPUT_TOKENS * m.input_usd + HOUR_OUTPUT_TOKENS * m.output_usd) / 1e6).toFixed(2)})`,
              }))}
            />
          </Row>
        </Section>

        <Section icon={<KeyRound className="size-3.5" />} title="API keys">
          {KEYS.map((key) => (
            <KeyRow key={key.name} {...key} status={keys?.[key.name]} onSaved={() => void refreshKeys()} />
          ))}
        </Section>

        <Section icon={<Keyboard className="size-3.5" />} title="Shortcuts">
          <Row label="Start or stop recording" help="Works from anywhere, even with ZachCap in the tray.">
            <kbd className="rounded-md border bg-muted px-2 py-1 font-mono text-xs">Ctrl + Alt + R</kbd>
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
              <Button size="sm" variant="outline" onClick={() => void invoke("open_folder", { path: health.data })}>
                <FolderOpen className="size-3.5" />
                Open
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
