import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useRef } from "react";

import { usePoll } from "@/hooks/usePoll";
import { api, isTauri, type Settings } from "@/lib/engine";
import { announce } from "@/lib/feedback";

type Dictated = { text: string; seconds: number; took: number; typed: number; copied: boolean };

/** Hold Ctrl+Alt+D anywhere: the engine listens while the keys are down, then types what
 * was said where the cursor is. The shell registers the keys only while dictation is on in
 * Settings, and tells this page when they go down and up. */
export function DictationKeys() {
  const { data: settings } = usePoll(() => api<Settings>("/api/settings"), 10_000);
  const enabled = settings?.dictation ?? false;

  useEffect(() => {
    if (isTauri()) void invoke("set_dictation", { enabled }).catch(() => undefined);
  }, [enabled]);

  const listening = useRef(false);
  useEffect(() => {
    if (!isTauri()) return;
    const stops: (() => void)[] = [];
    let cancelled = false;
    const on = (event: string, handler: () => void) =>
      void listen(event, handler).then((stop) => {
        if (cancelled) stop();
        else stops.push(stop);
      });
    on("dictate-start", () => {
      listening.current = true;
      void api("/api/dictate/start", { method: "POST" }).catch((error) => {
        listening.current = false;
        void announce("Dictation didn't start", String(error), "error");
      });
    });
    on("dictate-stop", () => {
      if (!listening.current) return;
      listening.current = false;
      void api<Dictated>("/api/dictate/stop", { method: "POST", json: { type: true } })
        .then((result) => {
          if (!result.text) {
            void announce("Didn't catch that", "Nothing was heard while the keys were held.", "warning");
          } else if (result.copied) {
            // The window in front refused the keystrokes (it runs as administrator).
            void announce("Copied instead", "That window didn't take typing. Paste with Ctrl+V.", "warning");
          }
        })
        .catch((error) => void announce("Dictation failed", String(error), "error"));
    });
    return () => {
      cancelled = true;
      stops.forEach((stop) => stop());
    };
  }, []);

  return null;
}
