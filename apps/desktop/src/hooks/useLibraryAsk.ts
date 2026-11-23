import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import type { CitedMeetings } from "@/components/Answer";
import { api } from "@/lib/engine";

export type LibraryExchange = {
  id: number;
  question: string;
  answer?: string;
  meetings?: CitedMeetings;
  cost?: number;
  model?: string;
  failed?: boolean;
};

type LibraryAnswer = { answer: string; meetings: CitedMeetings; cost_usd: number; model: string };

export type LibraryAsk = {
  history: LibraryExchange[];
  busy: boolean;
  /** False when another question is still being answered. */
  ask: (text: string) => boolean;
  reset: () => void;
};

/** The conversation on the Ask page. It lives in the app shell, not the page, so an answer
 * still on its way lands under its own question even if the page is left or reopened. */
export function useLibraryAsk(): LibraryAsk {
  const [history, setHistory] = useState<LibraryExchange[]>([]);
  const latest = useRef(history);
  useEffect(() => {
    latest.current = history;
  }, [history]);
  const inFlight = useRef(false);
  const nextId = useRef(1);

  const ask = useCallback((text: string) => {
    const question = text.trim();
    if (!question) return true;
    if (inFlight.current) return false;
    inFlight.current = true;
    const id = nextId.current++;
    // Earlier answers go along, so follow-up questions ("and Mark?") make sense.
    const earlier = latest.current
      .filter((e) => e.answer)
      .slice(-6)
      .map((e) => ({ question: e.question, answer: e.answer! }));
    const update = (patch: Partial<LibraryExchange>) =>
      setHistory((h) => h.map((e) => (e.id === id ? { ...e, ...patch } : e)));
    setHistory((h) => [...h, { id, question }]);
    void api<LibraryAnswer>("/api/ask", { method: "POST", json: { question, history: earlier } })
      .then((result) => update({ answer: result.answer, meetings: result.meetings, cost: result.cost_usd, model: result.model }))
      .catch((error) => {
        toast.error("Couldn't get an answer", { description: String(error) });
        update({ failed: true });
      })
      .finally(() => {
        inFlight.current = false;
      });
    return true;
  }, []);

  const reset = useCallback(() => setHistory([]), []);
  const busy = history.some((e) => !e.answer && !e.failed);
  return { history, busy, ask, reset };
}
