import { describe, expect, it } from "vitest";

import { choiceList } from "./choices";

/** A save that answers when the test says so. */
function slowSaves() {
  const sent: string[][] = [];
  const answers: ((ok: boolean) => void)[] = [];
  const save = (list: string[]) =>
    new Promise<void>((resolve, reject) => {
      sent.push(list);
      answers.push((ok) => (ok ? resolve() : reject(new Error("refused"))));
    });
  return { sent, answers, save };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("a list of choices saved in the background", () => {
  it("builds each click on the one before, and saves them one at a time, in order", async () => {
    const { sent, answers, save } = slowSaves();
    const list = choiceList(save);
    const saved = ["3CX", "Microsoft Teams"];
    expect(list.toggle(saved, "Slack")).toEqual(["3CX", "Microsoft Teams", "Slack"]);
    expect(list.toggle(saved, "Discord")).toEqual(["3CX", "Microsoft Teams", "Slack", "Discord"]);
    expect(list.shown(saved)).toEqual(["3CX", "Microsoft Teams", "Slack", "Discord"]);
    await settle();
    expect(sent).toEqual([["3CX", "Microsoft Teams", "Slack"]]); // the second waits its turn
    answers[0](true);
    await settle();
    expect(sent[1]).toEqual(["3CX", "Microsoft Teams", "Slack", "Discord"]);
    answers[1](true);
    await settle();
    expect(list.shown(["3CX", "Discord", "Microsoft Teams", "Slack"])).toEqual(["3CX", "Discord", "Microsoft Teams", "Slack"]);
  });

  it("undoes a quick second click on the same choice", async () => {
    const { sent, answers, save } = slowSaves();
    const list = choiceList(save);
    list.toggle(["Zoom"], "Slack");
    expect(list.toggle(["Zoom"], "Slack")).toEqual(["Zoom"]);
    await settle();
    answers[0](true);
    await settle();
    answers[1](true);
    await settle();
    expect(sent).toEqual([["Zoom", "Slack"], ["Zoom"]]);
  });

  it("goes on after a refused save, and then shows what was saved", async () => {
    const { sent, answers, save } = slowSaves();
    const changed: number[] = [];
    const list = choiceList(save, () => changed.push(1));
    list.toggle(["Zoom"], "Slack");
    list.toggle(["Zoom"], "Webex");
    await settle();
    answers[0](false);
    await settle();
    expect(sent.length).toBe(2);
    answers[1](true);
    await settle();
    expect(list.shown(["Zoom", "Slack", "Webex"])).toEqual(["Zoom", "Slack", "Webex"]);
    expect(changed.length).toBeGreaterThan(0); // the page is told to show the saved list again
  });
});
