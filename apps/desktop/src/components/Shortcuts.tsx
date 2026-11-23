import { invoke } from "@tauri-apps/api/core";
import { useEffect, useRef, useState } from "react";

import { SETTINGS_CHANGED } from "@/components/Recording";
import { Button } from "@/components/ui/button";
import { usePoll } from "@/hooks/usePoll";
import { api, isTauri, type Settings } from "@/lib/engine";
import { keysFromEvent, setKeys, showKeys } from "@/lib/shortcuts";

/** Passes the keys picked in Settings, and whether dictation is on, to the shell, which
 * registers the global shortcuts: at start and on every change. */
export function GlobalKeys() {
  const { data: settings, refresh } = usePoll(() => api<Settings>("/api/settings"), 10_000);
  const keys = settings?.shortcuts;

  // Changed in Settings (dictation turned on or off): the keys follow at once.
  useEffect(() => {
    const changed = () => void refresh();
    window.addEventListener(SETTINGS_CHANGED, changed);
    return () => window.removeEventListener(SETTINGS_CHANGED, changed);
  }, [refresh]);
  const dictation = settings?.dictation ?? false;
  const picked = keys ? JSON.stringify(keys) : null;

  useEffect(() => {
    if (!picked) return;
    const keys = JSON.parse(picked) as Settings["shortcuts"];
    setKeys(keys);
    if (isTauri()) void invoke("set_shortcuts", { keys }).catch(() => undefined);
  }, [picked]);

  useEffect(() => {
    if (isTauri()) void invoke("set_dictation", { enabled: dictation }).catch(() => undefined);
  }, [dictation]);

  return null;
}

/** The button waiting for keys now, if any: only one waits at a time. */
let stopWaiting: (() => void) | null = null;

/** A shortcut's keys, as a button: click it and press new keys (Esc to cancel). ZachCap lets
 * go of its own shortcuts meanwhile, so pressing one of them reaches this page. */
export function KeysButton({ label, keys, onPick }: { label: string; keys: string; onPick: (keys: string) => void }) {
  const [listening, setListening] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const pick = useRef(onPick);
  pick.current = onPick;

  useEffect(() => {
    if (!listening) return;
    setProblem(null);
    const stop = () => setListening(false);
    stopWaiting?.(); // another button was waiting: it stops
    stopWaiting = stop;
    if (isTauri()) void invoke("pause_shortcuts", { paused: true }).catch(() => undefined);
    const onKey = (event: KeyboardEvent) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.key === "Escape" && !event.ctrlKey && !event.altKey && !event.metaKey) {
        setListening(false);
        return;
      }
      const result = keysFromEvent(event);
      if (!result) return; // only modifiers so far
      if ("problem" in result) {
        setProblem(result.problem);
        return;
      }
      setListening(false);
      pick.current(result.keys);
    };
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("blur", stop);
    return () => {
      if (stopWaiting === stop) stopWaiting = null;
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("blur", stop);
      // Picked keys are passed on by the save; otherwise ZachCap takes its own back.
      if (isTauri()) void invoke("pause_shortcuts", { paused: false }).catch(() => undefined);
    };
  }, [listening]);

  return (
    <div className="flex min-w-0 flex-col gap-1">
      <Button
        size="sm"
        variant="outline"
        aria-label={listening ? `Press the new keys for ${label}` : `${label}: ${keys}. Change`}
        className="justify-start font-mono text-xs"
        onClick={() => setListening((on) => !on)}
      >
        {listening ? "Press the new keys..." : showKeys(keys)}
      </Button>
      {listening && (
        <p className="text-xs text-muted-foreground" aria-live="polite">
          {problem ?? "Hold Ctrl, Alt or Win and press a letter, a digit or an F key (nothing happens if another app has them). Esc cancels."}
        </p>
      )}
    </div>
  );
}
