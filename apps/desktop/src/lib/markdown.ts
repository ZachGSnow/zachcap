import type { ActionItem, Summary } from "./engine";

/** What makes a Markdown app (Obsidian, say) load something when it opens a note: an image,
 * inline, by reference or embedded ("![x](url)", "![x][1]", "![[x]]"), or raw HTML, which
 * starts with "<" and a letter, "/", "!" or "?" (a tag, a comment, "<https://...>"). The
 * backslashes already in front of one come along, as they decide whether it is escaped. The
 * engine's export.inert() follows the same rule for the files it writes. */
const LOADS = /(\\*)(!\[|<(?=[A-Za-z/!?]))/g;

/** Text from a meeting or from Claude, for Markdown: a backslash goes before each "![" and
 * each "<" that could start HTML, so a Markdown app shows them as typed and loads nothing (an
 * image's link stays a link). Backslashes already in front are doubled, so they still show
 * and can't cancel it. */
export function inert(text: string): string {
  return text.replace(LOADS, (_found, slashes: string, start: string) => slashes + slashes + "\\" + start);
}

/** A summary as Markdown, for the clipboard. The headings, checkboxes and bold are the page's
 * own; every title, name and text from the meeting or from Claude goes through inert(). */
export function summaryMarkdown(title: string, summary: Summary, items: ActionItem[]): string {
  const c = summary.content;
  const list = (values: string[]) => values.map((value) => `- ${inert(value)}`).join("\n");
  const sections = [
    `# ${inert(title)}`,
    inert(c.overview),
    items.length
      ? `## Action items\n${items
          .map(
            (a) =>
              `- [${a.status === "done" ? "x" : " "}] **${inert(a.owner)}**: ${inert(a.task)}${a.due ? ` (${inert(a.due)})` : ""}`,
          )
          .join("\n")}`
      : "",
    c.decisions.length ? `## Decisions\n${list(c.decisions)}` : "",
    c.who_said_what?.length
      ? `## Who said what\n${c.who_said_what.map((s) => `**${inert(s.speaker)}**\n${list(s.points)}`).join("\n\n")}`
      : "",
    c.open_questions.length ? `## Open questions\n${list(c.open_questions)}` : "",
  ];
  return sections.filter(Boolean).join("\n\n");
}
