/** A list of choices changed by clicks and saved in the background (the call apps that record
 * on their own). Each click builds on the one before it, not on the list the last save sent
 * back, and the saves go out one at a time, in order: a quick second click is never lost, and
 * an earlier save never lands after a later one. ``changed`` is called when what to show
 * changes back to the saved list (every save done). */
export function choiceList(save: (list: string[]) => Promise<unknown>, changed: () => void = () => undefined) {
  let clicked: string[] | null = null; // ahead of the saved list while saves are under way
  let queue: Promise<unknown> = Promise.resolve();
  let clicks = 0;
  return {
    /** What to show: the clicked list while it's being saved, else the saved one. */
    shown(saved: string[]): string[] {
      return clicked ?? saved;
    },
    /** Adds or removes ``item`` (from the list as last clicked) and saves the result. */
    toggle(saved: string[], item: string): string[] {
      const current = clicked ?? saved;
      const next = current.includes(item) ? current.filter((choice) => choice !== item) : [...current, item];
      clicked = next;
      const click = ++clicks;
      queue = queue
        .then(() => save(next))
        .catch(() => undefined) // said by the save itself; the next one still goes
        .then(() => {
          if (click !== clicks) return; // a later click is still to be saved
          clicked = null;
          changed();
        });
      return next;
    },
  };
}
