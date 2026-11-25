import { describe, expect, it } from "vitest";

import changelog from "../../../../CHANGELOG.md?raw";
import { version } from "../../package.json";
import { parseChangelog, pieces } from "./changelog";

describe("parseChangelog", () => {
  it("reads versions, what they're about, and their lists", () => {
    const text = [
      "# Changelog",
      "",
      "Not part of any version.",
      "",
      "## Unreleased",
      "",
      "### New",
      "",
      "- Coming.",
      "",
      "## 1.2.0 - 2026-01-02",
      "",
      "One line about it,",
      "and another.",
      "",
      "### New",
      "",
      "- **Something.** It works.",
      "- Something else.",
      "",
      "### Fixed",
      "",
      "- A fault.",
    ].join("\r\n");
    expect(parseChangelog(text)).toEqual([
      { version: null, date: null, about: "", sections: [{ title: "New", items: ["Coming."] }] },
      {
        version: "1.2.0",
        date: "2026-01-02",
        about: "One line about it, and another.",
        sections: [
          { title: "New", items: ["**Something.** It works.", "Something else."] },
          { title: "Fixed", items: ["A fault."] },
        ],
      },
    ]);
  });

  it("takes nothing from a text without versions", () => {
    expect(parseChangelog("# Changelog\n\n### New\n\n- Nothing.")).toEqual([]);
  });
});

describe("CHANGELOG.md", () => {
  const releases = parseChangelog(changelog);
  const released = releases.filter((release) => release.version !== null);

  it("starts with this version, or with what comes after it", () => {
    expect(released[0].version).toBe(version);
    expect(releases.indexOf(released[0])).toBeLessThanOrEqual(1);
  });

  it("has no heading the app would leave out", () => {
    const headings = changelog.split("\n").filter((line) => line.startsWith("## "));
    expect(headings).toHaveLength(releases.length);
  });

  it("goes back version by version, each with something to say", () => {
    const number = (release: { version: string | null }) =>
      release.version!.split(".").reduce((sum, part) => sum * 1000 + Number(part), 0);
    for (const [index, release] of released.entries()) {
      if (index > 0) expect(number(release)).toBeLessThan(number(released[index - 1]));
      expect(release.about).not.toBe("");
    }
  });

  it("only uses the headings it's known by", () => {
    const titles = releases.flatMap((release) => release.sections.map((section) => section.title));
    expect(titles.filter((title) => !["New", "Improved", "Fixed", "Security"].includes(title))).toEqual([]);
    for (const release of releases) for (const section of release.sections) expect(section.items).not.toEqual([]);
  });
});

describe("pieces", () => {
  it("finds bold and code", () => {
    expect(pieces("**New.** Run `uv sync` first")).toEqual([
      { text: "New.", style: "bold" },
      { text: " Run ", style: null },
      { text: "uv sync", style: "code" },
      { text: " first", style: null },
    ]);
  });

  it("leaves lone marks as they are", () => {
    expect(pieces("2 ** 3 and a ` tick")).toEqual([{ text: "2 ** 3 and a ` tick", style: null }]);
  });
});
