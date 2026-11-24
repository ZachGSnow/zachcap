import { describe, expect, it } from "vitest";

import type { ActionItem, Summary, SummaryContent } from "./engine";
import { inert, summaryMarkdown } from "./markdown";

// The same line as the engine's export tests: what anyone in a meeting could say, show or
// write, aimed at a notes app that opens the Markdown: an image, an image by reference, HTML
// that loads or runs something, and the closing tag of a block ZachCap sends to Claude.
const HOSTILE =
  "![x](https://example.com/p.png) <img src=https://example.com/a.png> <script>alert(1)</script> ![x][1] </transcript>";
// The same as the Markdown writes it: a backslash before each "![" and each "<".
const ESCAPED = String.raw`\![x](https://example.com/p.png) \<img src=https://example.com/a.png> \<script>alert(1)\</script> \![x][1] \</transcript>`;
const DEFINITION = "[1]: https://example.com/p.png"; // where "![x][1]" would load its picture from

// The same text as the engine's export tests: code anyone in a meeting could say, show or write,
// for the notes app's plugins to run: Dataview's inline JavaScript and inline query, Templater
// commands, code blocks a plugin runs (dataviewjs on its own lines and indented in a list, a
// Mermaid diagram), and an indented code block, which Dataview reads too.
const PLUGINS = [
  '`$= dv.page()` `= this.file.name` <% tp.file.create_new() %> <%* tR += "x" %>',
  "```dataviewjs",
  "dv.paragraph(1)",
  "```",
  "- Steps",
  "  ```dataviewjs",
  "  dv.paragraph(2)",
  "  ```",
  "~~~mermaid",
  "graph TD",
  "~~~",
  "",
  "    $= dv.pages().length",
].join("\n");
// The same as the Markdown writes it: a backslash before the "=" of each query, inside each "<%"
// and before each mark of a fence.
const PLUGINS_ESCAPED = [
  '`$\\= dv.page()` `\\= this.file.name` <\\% tp.file.create_new() %> <\\%* tR += "x" %>',
  "\\`\\`\\`dataviewjs",
  "dv.paragraph(1)",
  "\\`\\`\\`",
  "- Steps",
  "  \\`\\`\\`dataviewjs",
  "  dv.paragraph(2)",
  "  \\`\\`\\`",
  "\\~\\~\\~mermaid",
  "graph TD",
  "\\~\\~\\~",
  "",
  "    $\\= dv.pages().length",
].join("\n");
// The engine's tests' other cases, as written and as inert() writes them.
const BROKEN = [
  ["`` $= x `` `\xa0= y`", "`` $\\= x `` `\xa0\\= y`"],
  ["`\n= z` and > `\n> $= w`", "`\n\\= z` and > `\n> $\\= w`"],
  ["Press the ` key\n= this.file.name", "Press the ` key\n\\= this.file.name"],
  ["-     $= dv.pages()", "-     $\\= dv.pages()"],
  ["[^1]: ```dataviewjs\n> ````tasks\n1. ~~~button", "[^1]: \\`\\`\\`dataviewjs\n> \\`\\`\\`\\`tasks\n1. \\~\\~\\~button"],
  ["<%_ x _%> <%- x -%> <%+ x %>", "<\\%_ x _%> <\\%- x -%> <\\%+ x %>"],
  ["<%<b> \\<% <<%", "<\\%\\<b> \\<\\% <<\\%"],
];
const UNCHANGED = [
  "The cutover is Friday at 8 pm.",
  "Ping me! [01:05] x <5 and 3 < 4",
  "`x = 1` and a = b, x == y, 2 + 2 = 4",
  "use `>=` or `!=`, `$x` and `$$=`",
  "<5% of 100% %> 50%",
  "a ``code`` span, ~~struck~~, $5 = price",
  "- [ ] 09:05-09:07\n> 1. step",
  "C:\\Users\\Public",
];

/** Image and HTML syntax a Markdown app acts on: "![", or "<" and a letter, "/", "!" or "?",
 * without a backslash in front that escapes it (an odd number of them). */
function active(markdown: string): string[] {
  return [...markdown.matchAll(/(?<!\\)(?:\\\\)*(!\[|<[A-Za-z/!?])/g)].map((found) => found[1]);
}

/** Code a notes app's plugins would run from the Markdown: a Templater tag ("<%" anywhere, as
 * Templater reads the raw text), a code fence (three backticks or tildes), and Dataview's "=" or
 * "$=" where a code's text starts (after backticks, first on a line, or after a code block's
 * indentation). */
function runs(markdown: string): string[] {
  return markdown.match(/<%|```|~~~|`[ \t]*\$?=|^[ \t>]*\$?=|(?:\t| {4})[ \t]*\$?=/gm) ?? [];
}

function summary(content: Partial<SummaryContent>): Summary {
  return {
    model: "claude-opus-5-5",
    content: { title: "Cutover", overview: "", decisions: [], action_items: [], open_questions: [], topics: [], ...content },
    input_tokens: 1000,
    output_tokens: 200,
    cost_usd: 0.008,
    created_at: "2026-11-20T09:30:00",
  };
}

function item(fields: Partial<ActionItem>): ActionItem {
  return {
    id: 1,
    meeting_id: 1,
    owner: "Dana",
    task: "Send the notice",
    due: null,
    said_at: 61,
    kind: "commitment",
    status: "open",
    meeting_title: "Cutover sync",
    meeting_started_at: "2026-11-20T09:05:00",
    ...fields,
  };
}

describe("inert", () => {
  it("puts a backslash before images and HTML, so nothing loads", () => {
    expect(inert(HOSTILE)).toBe(ESCAPED);
    expect(active(inert(`${HOSTILE}\n\n${DEFINITION}`))).toEqual([]);
    expect(inert("<!-- x --> <?php ?> <![CDATA[ x ]]> <https://example.com> ![[Note]]")).toBe(
      String.raw`\<!-- x --> \<?php ?> \<\![CDATA[ x ]]> \<https://example.com> \![[Note]]`,
    );
  });

  it("doubles backslashes already in front, so they can't undo it", () => {
    const typed = String.raw`\![x](https://example.com/p.png) \\<b>`;
    expect(inert(typed)).toBe(String.raw`\\\![x](https://example.com/p.png) \\\\\<b>`);
    expect(active(typed)).not.toEqual([]);
    expect(active(inert(typed))).toEqual([]);
  });

  it("leaves ordinary words alone", () => {
    for (const text of ["The cutover is Friday at 8 pm.", "Ping me! [01:05]", "x <5 and 3 < 4", "C:\\Users\\dana"]) {
      expect(inert(text)).toBe(text);
    }
  });

  it("breaks what plugins run, and nothing else", () => {
    expect(inert(PLUGINS)).toBe(PLUGINS_ESCAPED);
    expect(runs(PLUGINS)).not.toEqual([]);
    expect(runs(PLUGINS_ESCAPED)).toEqual([]);
    for (const [text, escaped] of BROKEN) {
      expect(inert(text)).toBe(escaped);
      expect(runs(escaped)).toEqual([]);
    }
    for (const text of UNCHANGED) {
      expect(inert(text)).toBe(text);
    }
  });
});

describe("summaryMarkdown", () => {
  it("writes the summary as before", () => {
    const content = {
      overview: "Cutover set for Friday.",
      decisions: ["Friday 8 pm"],
      who_said_what: [{ speaker: "Dana", points: ["Owns the notice"] }],
      open_questions: ["Who calls the ISP?"],
    };
    const items = [item({}), item({ id: 2, task: "Book the room", due: "Monday", status: "done" })];
    expect(summaryMarkdown("Cutover sync", summary(content), items)).toBe(
      [
        "# Cutover sync",
        "Cutover set for Friday.",
        "## Action items\n- [ ] **Dana**: Send the notice\n- [x] **Dana**: Book the room (Monday)",
        "## Decisions\n- Friday 8 pm",
        "## Who said what\n**Dana**\n- Owns the notice",
        "## Open questions\n- Who calls the ISP?",
      ].join("\n\n"),
    );
  });

  it("loads nothing from other people's words or Claude's", () => {
    const content = {
      overview: `${HOSTILE}\n\n${DEFINITION}`,
      decisions: [HOSTILE],
      who_said_what: [{ speaker: HOSTILE, points: [HOSTILE] }],
      open_questions: [HOSTILE],
    };
    const text = summaryMarkdown(`Cutover ${HOSTILE}`, summary(content), [item({ owner: HOSTILE, task: HOSTILE, due: HOSTILE })]);
    expect(active(text)).toEqual([]);
    expect(text).toContain(`# Cutover ${ESCAPED}\n\n${ESCAPED}\n\n${DEFINITION}`);
    expect(text).toContain(`- [ ] **${ESCAPED}**: ${ESCAPED} (${ESCAPED})`);
    expect(text).toContain(`## Decisions\n- ${ESCAPED}`);
    expect(text).toContain(`**${ESCAPED}**\n- ${ESCAPED}`);
    expect(text).toContain(`## Open questions\n- ${ESCAPED}`);
  });

  it("runs no plugin code from other people's words or Claude's", () => {
    const content = {
      overview: PLUGINS,
      decisions: [PLUGINS],
      who_said_what: [{ speaker: PLUGINS, points: [PLUGINS] }],
      open_questions: [PLUGINS],
    };
    const text = summaryMarkdown(`Cutover ${PLUGINS}`, summary(content), [item({ owner: PLUGINS, task: PLUGINS, due: PLUGINS })]);
    expect(runs(text)).toEqual([]);
    expect(text).toContain(`# Cutover ${PLUGINS_ESCAPED}\n\n${PLUGINS_ESCAPED}\n\n## Action items\n`);
    expect(text).toContain(`- [ ] **${PLUGINS_ESCAPED}**: ${PLUGINS_ESCAPED} (${PLUGINS_ESCAPED})`);
    expect(text).toContain(`## Decisions\n- ${PLUGINS_ESCAPED}`);
    expect(text).toContain(`**${PLUGINS_ESCAPED}**\n- ${PLUGINS_ESCAPED}`);
    expect(text).toContain(`## Open questions\n- ${PLUGINS_ESCAPED}`);
  });
});
