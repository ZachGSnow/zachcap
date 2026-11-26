/** What a summary keeps beside its text: the second each decision and open question was said,
 * and the lasting facts Claude noticed, each with the words it came from. Summaries from before
 * 0.6.0, and those written on this PC, have none of these. */

export type Fact = { about: string; name: string; value: string; said_at?: number | null; quote?: string | null };

/** The second the item at `index` was said, when the summary kept one. */
export function secondOf(seconds: readonly unknown[] | undefined, index: number): number | null {
  const at = seconds?.[index];
  return typeof at === "number" && Number.isFinite(at) && at >= 0 ? at : null;
}

/** The facts a summary kept that can be shown: whom each is about, what, and the value. */
export function factsOf(facts: unknown): Fact[] {
  if (!Array.isArray(facts)) return [];
  return facts.filter(
    (fact): fact is Fact =>
      typeof fact === "object" &&
      fact !== null &&
      ["about", "name", "value"].every((key) => {
        const text = (fact as Record<string, unknown>)[key];
        return typeof text === "string" && text.trim() !== "";
      }),
  );
}

/** Whom a fact is about, as a person reads it: a speaker's name, or the client. */
export function factAbout(about: string): string {
  return about.trim().toLowerCase() === "client" ? "The client" : about.trim();
}

/** Words someone said, in quotes. */
export function quoted(words: string): string {
  return `"${words.trim()}"`;
}
