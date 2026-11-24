import type { Meeting } from "@/lib/engine";

/** The kind of meeting the summary's picker shows: one picked on the page, else the meeting's
 * own (``automatic`` when ZachCap picked it, so the page says so; ``unpicked`` when it is still
 * Automatic because no kind could be picked), else the usual kind from Settings ("auto" being
 * Automatic). Regenerate uses the kind shown. */
export function kindShown(
  picked: string | null,
  meeting: Pick<Meeting, "template" | "template_source">,
  usual: string | undefined,
): { value: string; automatic: boolean; unpicked: boolean } {
  if (picked !== null) return { value: picked, automatic: false, unpicked: false };
  if (meeting.template) {
    const automatic = meeting.template_source === "auto";
    return { value: meeting.template, automatic, unpicked: meeting.template === "auto" };
  }
  return { value: usual ?? "general", automatic: false, unpicked: false };
}

/** The pick to keep once a summary was written with ``sent``: none, so the picker shows the
 * kind it was written as (the one picked, after Automatic), unless another was picked since. */
export function pickAfterSummary(now: string | null, sent: string | null): string | null {
  return now === sent ? null : now;
}
