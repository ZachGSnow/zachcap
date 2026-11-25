/** A version in CHANGELOG.md. */
export type Release = {
  /** "0.4.0", or null for what isn't released yet. */
  version: string | null;
  /** The day it was released, as 2026-11-24. */
  date: string | null;
  /** What it's about, in a sentence or two. */
  about: string;
  sections: { title: string; items: string[] }[];
};

/** CHANGELOG.md's versions, in its order (newest first): "## 0.4.0 - 2026-11-24" or
 * "## Unreleased", what it's about, and lists under "### New" and the like. What comes before
 * the first version isn't part of any. */
export function parseChangelog(text: string): Release[] {
  const releases: Release[] = [];
  for (const line of text.split(/\r?\n/)) {
    const heading = /^## (?:Unreleased|(\d+\.\d+\.\d+) - (\d{4}-\d{2}-\d{2}))\s*$/.exec(line);
    if (heading) {
      releases.push({ version: heading[1] ?? null, date: heading[2] ?? null, about: "", sections: [] });
      continue;
    }
    const release = releases.at(-1);
    if (!release) continue;
    const section = release.sections.at(-1);
    if (line.startsWith("### ")) release.sections.push({ title: line.slice(4).trim(), items: [] });
    else if (line.startsWith("- ")) section?.items.push(line.slice(2).trim());
    else if (line.trim() && !section) release.about = `${release.about} ${line.trim()}`.trim();
  }
  return releases;
}

/** A line's text in pieces: **bold**, `code`, and the rest. */
export function pieces(text: string): { text: string; style: "bold" | "code" | null }[] {
  return text
    .split(/(\*\*[^*]+\*\*|`[^`]+`)/)
    .filter(Boolean)
    .map((part) =>
      part.startsWith("**") && part.length > 4
        ? { text: part.slice(2, -2), style: "bold" }
        : part.startsWith("`") && part.length > 2
          ? { text: part.slice(1, -1), style: "code" }
          : { text: part, style: null },
    );
}
