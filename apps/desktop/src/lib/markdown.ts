import type { ActionItem, Summary } from "./engine";

/** What makes a Markdown app (Obsidian, say) load something, or one of its plugins run code,
 * when it opens a note, and the backslash that stops each:
 * - An image, inline, by reference or embedded ("![x](url)", "![x][1]", "![[x]]"), or raw HTML,
 *   which starts with "<" and a letter, "/", "!" or "?" (a tag, a comment, "<https://...>"): a
 *   backslash before it. The backslashes already in front of one come along, from the first of
 *   them (so a long run is read once), and are doubled, as they decide whether it is escaped.
 * - "<%", which opens a Templater command wherever it is: Templater reads the file's raw text
 *   (code included) when a new file appears, and has no escape. It is written "<\%".
 * - Three or more backticks or tildes, which open a code block that Obsidian or a plugin runs
 *   (mermaid, dataviewjs, dataview, tasks, button...): a backslash before each. Anywhere, not
 *   only at the start of a line, as a quote, a list item or a footnote ("[^1]: ") can open one
 *   mid-line.
 * - "=" or "$=" where a code's text can start, which Dataview runs as a query (it trims the
 *   text first, and reads code blocks too): after backticks and any spaces; first on a line,
 *   after any spaces and a quote's ">" (a code can go on from the line before, and an editor
 *   may take a backtick left open as the start of one); or after a tab or four spaces (an
 *   indented code block). A backslash goes before the "=": inside code it shows, so the text
 *   no longer starts with Dataview's prefix; elsewhere Markdown hides it.
 * BLANK is the whitespace JavaScript's trim() removes, other than line breaks, spelled out
 * (Python's \s differs). The engine's export.inert() follows the same rule for the files it
 * writes. */
const BLANK = String.raw`[\t\v\f \xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]`; // no line break
const LINE = String.raw`(?:^|(?<=[\n\r]))(?:` + BLANK + "|>)*"; // a line's start, its spaces and quotes
const INDENT = String.raw`(?: {4}| {0,3}\t)`; // an indented code block's: four spaces, or a tab after fewer
const ACTIVE = new RegExp(
  [
    // An image or HTML, and the backslashes in front of it
    String.raw`(?<!\\)(\\*)(!\[|<(?=[A-Za-z/!?]))`,
    // A Templater command
    "<%",
    // A code fence
    "(`{3,}|~{3,})",
    // Dataview's prefix at the start of a code's text
    "(`+" + BLANK + "*|" + LINE + "|(?<!" + BLANK + ")" + INDENT + BLANK + "*)([$]?)=",
  ].join("|"),
  "g",
);

/** What ACTIVE found, with its backslashes. */
function escaped(_found: string, slashes: string, start?: string, fence?: string, code?: string, dollar?: string) {
  if (start) return slashes + slashes + "\\" + start;
  if (fence) return [...fence].map((mark) => "\\" + mark).join("");
  if (code !== undefined) return code + dollar + "\\=";
  return "<\\%";
}

/** Text from a meeting or from Claude, for Markdown: a backslash goes before each "![" and
 * each "<" that could start HTML, inside each "<%", before each mark of a code fence and before
 * the "=" that starts a Dataview query, so a Markdown app shows them as typed (an image's link
 * stays a link, a code block's code a paragraph) and neither it nor its plugins load or run any
 * of it. Backslashes already in front of "![" or "<" are doubled, so they still show and can't
 * cancel it. */
export function inert(text: string): string {
  return text.replace(ACTIVE, escaped);
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
