import type { WordFound } from "@/lib/engine";

/** The ticks with one put on, or taken off. */
export function toggled(ticked: ReadonlySet<string>, term: string): ReadonlySet<string> {
  const next = new Set(ticked);
  if (!next.delete(term)) next.add(term);
  return next;
}

/** The ticked ones in the order they're listed (the most useful first), as they're added. */
export function chosenWords(found: WordFound[], ticked: ReadonlySet<string>): string[] {
  return found.filter(({ term }) => ticked.has(term)).map(({ term }) => term);
}

/** What the button adds. */
export function addLabel(count: number): string {
  if (!count) return "Add words";
  return count === 1 ? "Add 1 word" : `Add ${count} words`;
}

/** How often a term is in the file. */
export function timesFound(count: number): string {
  return count === 1 ? "once" : `${count} times`;
}

/** Why the chosen ones don't fit in the list (``room`` more characters), or null when they do.
 * Each goes on a line of its own. */
export function noRoom(chosen: string[], room: number): string | null {
  const needed = chosen.reduce((sum, term) => sum + term.length + 1, 0);
  if (needed <= room) return null;
  return `These need ${needed} characters, and Words to know has room for ${room}. Untick some, or take some out of the list first.`;
}

const linesOf = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

/** What the Words to know box holds once the saved list has changed under it: the list as saved;
 * or, with edits that aren't saved yet, those edits with the lines the list gained at the end. */
export function carriedOver(edited: string, before: string, after: string): string {
  if (edited.trim() === before.trim() || edited.trim() === after.trim()) return after;
  const there = new Set([...linesOf(before), ...linesOf(edited)]);
  const gained = linesOf(after).filter((line) => !there.has(line));
  return gained.length ? [edited.trimEnd(), ...gained].filter(Boolean).join("\n") : edited;
}
