import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useRef } from "react";

import { SETTINGS_CHANGED } from "@/components/Recording";
import { usePoll } from "@/hooks/usePoll";
import { api, isTauri, type Settings } from "@/lib/engine";
import { announce } from "@/lib/feedback";

type Dictated = {
  text: string;
  seconds: number;
  took: number;
  typed: number;
  copied: boolean;
  /** Why it wasn't typed (it was copied instead), or null when it was. */
  why: "nowhere" | "keys" | "switched" | "admin" | "refused" | null;
};

const NOT_TYPED: Record<NonNullable<Dictated["why"]>, string> = {
  nowhere: "No window was in front to type into.",
  keys: "Keys were still held down when it was ready.",
  switched: "You switched windows before it was ready, so it wasn't typed there.",
  admin: "That window runs as administrator, so Windows won't let ZachCap type into it.",
  refused: "That window didn't take all of the typing.",
};

/** Hold Ctrl+Alt+D anywhere: the engine listens while the keys are down, then types what
 * was said where the cursor is. The shell registers the keys only while dictation is on in
 * Settings, and tells this page when they go down and up. */
export function DictationKeys() {
  const { data: settings, refresh } = usePoll(() => api<Settings>("/api/settings"), 10_000);
  const enabled = settings?.dictation ?? false;

  // Turned on or off in Settings: the keys follow at once, not at the next look.
  useEffect(() => {
    const changed = () => void refresh();
    window.addEventListener(SETTINGS_CHANGED, changed);
    return () => window.removeEventListener(SETTINGS_CHANGED, changed);
  }, [refresh]);

  useEffect(() => {
    if (isTauri()) void invoke("set_dictation", { enabled }).catch(() => undefined);
  }, [enabled]);

  // The dictation the keys started: its number, once the engine has answered.
  const started = useRef<Promise<number | null> | null>(null);
  useEffect(() => {
    if (!isTauri()) return;
    // A page that loads (or reloads mid-dictation) closes any mic its last copy left open.
    void api("/api/dictate/cancel", { method: "POST" }).catch(() => undefined);
    const stops: (() => void)[] = [];
    let cancelled = false;
    const on = (event: string, handler: () => void) =>
      void listen(event, handler).then((stop) => {
        if (cancelled) stop();
        else stops.push(stop);
      });
    on("dictate-start", () => {
      started.current = api<{ id: number }>("/api/dictate/start", { method: "POST" })
        .then((answer) => answer.id)
        .catch((error) => {
          void announce("Dictation didn't start", String(error), "error");
          return null;
        });
    });
    on("dictate-stop", () => {
      const dictation = started.current;
      started.current = null;
      if (!dictation) return;
      // Stop goes only after start has answered: a quick tap can't leave the mic open.
      void dictation.then(async (id) => {
        if (id === null) return;
        try {
          const result = await api<Dictated>("/api/dictate/stop", {
            method: "POST",
            json: { type: true, id },
          });
          if (!result.text) {
            void announce("Didn't catch that", "Nothing was heard while the keys were held.", "warning");
          } else if (result.why && result.copied) {
            void announce("Copied instead", `${NOT_TYPED[result.why]} Paste with Ctrl+V.`, "warning");
          } else if (result.why) {
            void announce("Couldn't type or copy it", `${NOT_TYPED[result.why]} You said: ${result.text}`, "error");
          }
        } catch (error) {
          void announce("Dictation failed", String(error), "error");
        }
      });
    });
    return () => {
      cancelled = true;
      stops.forEach((stop) => stop());
    };
  }, []);

  return null;
}
