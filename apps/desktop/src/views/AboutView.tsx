import { invoke } from "@tauri-apps/api/core";
import {
  AudioLines,
  ChevronRight,
  Copy,
  ExternalLink,
  FolderOpen,
  HardDrive,
  History,
  Layers,
  Lock,
  MonitorCheck,
  Scale,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { usePoll } from "@/hooks/usePoll";
import { parseChangelog, pieces, type Release } from "@/lib/changelog";
import { api, isTauri, type About } from "@/lib/engine";
import { attempt, copyText } from "@/lib/feedback";

import changelog from "../../../../CHANGELOG.md?raw";
import license from "../../../../LICENSE?raw";
import { version } from "../../package.json";

const RELEASES = parseChangelog(changelog);

/** What ZachCap is built on, with each one's license. */
const BUILT_ON = [
  ["Whisper large-v3-turbo", "what was said, through faster-whisper and CTranslate2", "MIT"],
  ["pyannote.audio", "who spoke when, and voices (speaker-diarization-community-1)", "MIT, CC-BY-4.0"],
  ["Silero VAD", "where there is speech", "MIT"],
  ["PyTorch", "runs the speaker models", "BSD-3-Clause"],
  ["PyAV and FFmpeg", "read the recordings you import", "BSD-3-Clause, LGPL-3.0"],
  ["Claude, by Anthropic", "summaries, documents and answers, with your own API key", "a service"],
  ["Ollama", "summaries on this PC, when you have it", "MIT"],
  ["Tauri", "the app around the page", "MIT or Apache-2.0"],
  ["React, Radix and shadcn/ui", "the page", "MIT"],
  ["Geist and Lucide", "its letters and icons", "OFL-1.1, ISC"],
  ["uv", "sets the speech engine up", "MIT or Apache-2.0"],
] as const;

const day = (date: string) => new Date(date).toLocaleDateString(undefined, { dateStyle: "long" });

/** The installer's stamp ("0.5.0 480063a 2026-11-24T23:35:02Z") as people read it. */
function buildText(stamp: string | null): string {
  if (!stamp) return "Run from its source";
  const [, commit, built] = stamp.split(" ");
  return built && !Number.isNaN(Date.parse(built)) ? `${commit}, built ${day(built)}` : stamp;
}

/** For a bug report: nothing of yours, such as folders or names, is in it. */
function details(about: About): string {
  return [
    `ZachCap ${version}${about.version === version ? "" : ` (engine ${about.version})`}`,
    `Build: ${buildText(about.build)}`,
    about.windows,
    `Python ${about.python}`,
    about.gpu === null ? null : `Speech on ${about.gpu ? "an NVIDIA GPU" : "the CPU"}`,
  ]
    .filter(Boolean)
    .join("\n");
}

function Heading({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <h2 className="mt-10 mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground">
      {icon}
      {children}
    </h2>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-4 px-4 py-2.5 text-sm">
      <dt className="w-32 shrink-0 text-muted-foreground">{label}</dt>
      <dd className="flex min-w-0 flex-1 items-center gap-2">{children}</dd>
    </div>
  );
}

function Where({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border bg-card p-4">
      <div className="flex items-center gap-2 text-sm font-medium">
        {icon}
        {title}
      </div>
      <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{children}</p>
    </div>
  );
}

function Line({ text }: { text: string }) {
  return (
    <>
      {pieces(text).map((piece, index) =>
        piece.style === "bold" ? (
          <strong key={index} className="font-medium text-foreground">
            {piece.text}
          </strong>
        ) : piece.style === "code" ? (
          <code key={index} className="font-mono text-xs">
            {piece.text}
          </code>
        ) : (
          <span key={index}>{piece.text}</span>
        ),
      )}
    </>
  );
}

/** One version's changes, under its number: open when it's the newest. */
function Version({ release, open }: { release: Release; open: boolean }) {
  return (
    <details open={open} className="group">
      <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 select-none hover:bg-muted/50 [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
        <span className="font-medium tabular-nums">{release.version ?? "Next"}</span>
        {release.version === version && <Badge variant="secondary">You have this one</Badge>}
        <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground group-open:invisible">{release.about}</span>
        <span className="shrink-0 text-xs text-muted-foreground">
          {release.date ? day(`${release.date}T00:00`) : "Not released yet"}
        </span>
      </summary>
      <div className="space-y-4 px-4 pt-1 pb-5 pl-11">
        <p className="text-sm">{release.about}</p>
        {release.sections.map((section) => (
          <section key={section.title}>
            <h4 className="text-xs font-medium text-muted-foreground">{section.title}</h4>
            <ul className="mt-1.5 space-y-1.5 text-sm leading-relaxed text-muted-foreground">
              {section.items.map((item) => (
                <li key={item} className="relative pl-4 before:absolute before:top-[0.6em] before:left-0.5 before:size-1 before:rounded-full before:bg-muted-foreground/60">
                  <Line text={item} />
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </details>
  );
}

/** What ZachCap is, what it keeps where, this installation, and what changed in each version. */
export function AboutView() {
  const { data: about } = usePoll(() => api<About>("/api/about"), 0);
  const tauri = isTauri();

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className="mx-auto max-w-3xl px-8 py-8">
        <div className="flex items-center gap-4">
          <div className="flex size-14 shrink-0 items-center justify-center rounded-2xl bg-foreground text-background shadow-sm">
            <AudioLines className="size-7" />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="font-heading text-2xl font-semibold tracking-tight">ZachCap</h1>
            <p className="text-sm text-muted-foreground">Meetings that remember who said what</p>
          </div>
          <div className="shrink-0 text-right">
            <div className="text-sm font-medium tabular-nums">Version {version}</div>
            {tauri && (
              <Button
                size="sm"
                variant="outline"
                className="mt-1.5"
                title="Opens ZachCap's releases on GitHub in your browser"
                onClick={() => void attempt("Couldn't open the browser", () => invoke("open_releases"))}
              >
                <ExternalLink className="size-3.5" />
                Newer versions
              </Button>
            )}
          </div>
        </div>

        <p className="mt-6 text-sm leading-relaxed text-muted-foreground">
          ZachCap records any call on this PC without a bot joining it, writes down what was said, tells the speakers apart
          and recognizes them by voice the next time. What was decided, promised and asked is in front of you afterwards, each
          item one click from the moment it was said.
        </p>

        <Heading icon={<ShieldCheck className="size-3.5" />}>What stays where</Heading>
        <div className="grid gap-3 sm:grid-cols-3">
          <Where icon={<HardDrive className="size-4" />} title="On this PC">
            Recordings, transcripts, voices, notes and the search index. Audio and voiceprints never leave it.
          </Where>
          <Where icon={<Sparkles className="size-4" />} title="With Claude">
            If you add an API key: the words of a meeting to summarize or answer from, with your notes and files. Never a
            private meeting.
          </Where>
          <Where icon={<Lock className="size-4" />} title="Or nothing at all">
            With a model in Ollama, summaries and documents are written on this PC as well, and nothing is sent.
          </Where>
        </div>

        <Heading icon={<History className="size-3.5" />}>What's new</Heading>
        <div className="divide-y overflow-hidden rounded-xl border bg-card">
          {RELEASES.map((release, index) => (
            <Version key={release.version ?? "next"} release={release} open={index === 0} />
          ))}
        </div>

        <Heading icon={<MonitorCheck className="size-3.5" />}>This installation</Heading>
        <dl className="divide-y overflow-hidden rounded-xl border bg-card">
          <Fact label="Version">
            <span className="tabular-nums">{version}</span>
            {about && about.version !== version && (
              <span className="text-muted-foreground">with the engine of {about.version}</span>
            )}
            {about && (
              <Button
                size="sm"
                variant="ghost"
                className="-my-1 ml-auto"
                title="The version, build and system, for a bug report. Nothing of yours is in it."
                onClick={() => void copyText(details(about), "Copied")}
              >
                <Copy className="size-3.5" />
                Copy details
              </Button>
            )}
          </Fact>
          <Fact label="Build">{about ? buildText(about.build) : "..."}</Fact>
          <Fact label="Speech engine">
            {about
              ? `Python ${about.python}${about.gpu === null ? "" : about.gpu ? ", on the NVIDIA GPU" : ", on the CPU"}`
              : "..."}
          </Fact>
          <Fact label="System">{about?.windows ?? "..."}</Fact>
          <Fact label="Data folder">
            <code className="min-w-0 flex-1 truncate font-mono text-xs" title={about?.data}>
              {about?.data ?? "..."}
            </code>
            {tauri && about && (
              <Button
                size="sm"
                variant="outline"
                className="-my-1"
                onClick={() => void attempt("Couldn't open the folder", () => invoke("open_data_folder"))}
              >
                <FolderOpen className="size-3.5" />
                Open
              </Button>
            )}
          </Fact>
        </dl>

        <Heading icon={<Layers className="size-3.5" />}>Built on</Heading>
        <ul className="divide-y overflow-hidden rounded-xl border bg-card">
          {BUILT_ON.map(([name, what, terms]) => (
            <li key={name} className="flex items-baseline gap-3 px-4 py-2 text-sm">
              <span className="min-w-0 flex-1">
                {name} <span className="text-xs text-muted-foreground">{what}</span>
              </span>
              <span className="shrink-0 text-xs text-muted-foreground">{terms}</span>
            </li>
          ))}
        </ul>

        <Heading icon={<Scale className="size-3.5" />}>License</Heading>
        <details className="group overflow-hidden rounded-xl border bg-card">
          <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 text-sm select-none hover:bg-muted/50 [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
            MIT license
            <span className="text-muted-foreground">Copyright (c) 2026 Zach Snow</span>
          </summary>
          <div className="space-y-3 px-4 pb-4 pl-11 text-xs leading-relaxed text-muted-foreground">
            {license
              .trim()
              .split(/\r?\n\s*\r?\n/)
              .map((paragraph) => (
                <p key={paragraph}>{paragraph.replace(/\s+/g, " ")}</p>
              ))}
          </div>
        </details>

        <p className="mt-8 text-xs leading-relaxed text-muted-foreground">
          Recording laws differ from place to place: tell people you're recording. Voiceprints are biometric data in some
          places.
        </p>
      </div>
    </ScrollArea>
  );
}
