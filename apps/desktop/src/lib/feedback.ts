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

/** Whether the user can see the window: not closed to the tray, not minimized. */
export async function windowShowing(): Promise<boolean> {
  if (document.visibilityState === "hidden") return false;
  if (!isTauri()) return true;
  try {
    const window = getCurrentWindow();
    return (await window.isVisible()) && !(await window.isMinimized());
  } catch {
    return true;
  }
}

/** A toast in the window, or a Windows notification while ZachCap sits in the tray. */
export async function announce(title: string, body?: string, kind: "info" | "success" | "error" = "info") {
  if (await windowShowing()) {
    const show = kind === "error" ? toast.error : kind === "success" ? toast.success : toast;
    show(title, body ? { description: body } : undefined);
  } else {
    await notify(title, body ?? "");
  }
}

/** Runs an action and says why when it fails, instead of failing silently. */
export async function attempt(failure: string, action: () => Promise<unknown>): Promise<boolean> {
  try {
    await action();
    return true;
  } catch (error) {
    toast.error(failure, { description: error instanceof Error ? error.message : String(error) });
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
