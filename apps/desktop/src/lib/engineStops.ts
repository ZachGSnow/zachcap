/** What the shell says when the engine stopped by itself (the engine-exited event). */
export type EngineExit = {
  /** Why, and where its log is. */
  message: string;
  /** The shell is starting it again (it does a few times, when it ran before). */
  restarting: boolean;
  /** It was recording. */
  recording: boolean;
};

/** What the banner says while the engine is stopped. */
export function stoppedText(exit: EngineExit): string {
  const kept = exit.recording ? "What it had recorded is kept. " : "";
  return kept + (exit.restarting ? "Starting it again..." : exit.message);
}

/** What the banner says once it was started again by itself. */
export function backText(recording: boolean): string {
  return recording
    ? "What it had recorded is kept. A call in Teams or another call app goes on in a new recording; if yours didn't, press Record."
    : "It wasn't recording.";
}
