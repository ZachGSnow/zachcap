import { getCurrentWindow } from "@tauri-apps/api/window";
import { toast } from "sonner";

import { isTauri } from "@/lib/engine";

/** A Windows notification, for when ZachCap's window isn't on screen. */
export async function notify(title: string, body: string) {
  if (!isTauri()) return;
  try {
    const { isPermissionGranted, requestPermission, sendNotification } = await import("@tauri-apps/plugin-notification");
    if (!(await isPermissionGranted()) && (await requestPermission()) !== "granted") return;
    sendNotification({ title, body });
  } catch {
    // Notifications are a nicety; the window shows the same thing when it is opened.
  }
}

/** Whether the user is looking at the window: shown (not in the tray or minimized) and in
 * front. Behind a full-screen Teams call it counts as unseen, so hotkeys get a notification. */
export async function windowInFront(): Promise<boolean> {
  if (document.visibilityState === "hidden") return false;
  if (!isTauri()) return true;
  try {
    const window = getCurrentWindow();
    return (await window.isVisible()) && !(await window.isMinimized()) && (await window.isFocused());
  } catch {
    return true;
  }
}

/** A toast in the window, or a Windows notification when the window isn't in front. */
export async function announce(
  title: string,
  body?: string,
  kind: "info" | "success" | "warning" | "error" = "info",
) {
  if (await windowInFront()) {
    const show = { info: toast, success: toast.success, warning: toast.warning, error: toast.error }[kind];
    show(title, body ? { description: body } : undefined);
  } else {
    await notify(title, body ?? "");
  }
}

/** What went wrong, as the engine said it ("This meeting is busy (redacting)..."), without
 * the "Error:" that String() puts in front. */
export function reason(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Runs an action and says why when it fails, instead of failing silently. */
export async function attempt(failure: string, action: () => Promise<unknown>): Promise<boolean> {
  try {
    await action();
    return true;
  } catch (error) {
    toast.error(failure, { description: reason(error) });
    return false;
  }
}

export async function copyText(text: string, done: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(done);
  } catch (error) {
    toast.error("Couldn't copy", { description: String(error) });
  }
}
