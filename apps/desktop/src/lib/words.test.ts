import { describe, expect, it } from "vitest";

import { addLabel, carriedOver, chosenWords, noRoom, timesFound, toggled } from "./words";

const found = [
  { term: "Dana Lee", count: 4 },
  { term: "ConnectWise", count: 12 },
  { term: "Proxmox", count: 1 },
];

describe("ticking words found in a file", () => {
  it("puts one on or takes it off, and leaves the ticks before it as they were", () => {
    const none: ReadonlySet<string> = new Set();
    const one = toggled(none, "Proxmox");
    const two = toggled(one, "Dana Lee");
    expect([...none]).toEqual([]);
    expect([...one]).toEqual(["Proxmox"]);
    expect([...toggled(two, "Proxmox")]).toEqual(["Dana Lee"]);
  });

  it("adds them in the order listed, whatever the order they were ticked in", () => {
    const ticked = toggled(toggled(new Set(), "Proxmox"), "Dana Lee");
    expect(chosenWords(found, ticked)).toEqual(["Dana Lee", "Proxmox"]);
    expect(chosenWords(found, new Set(["Fabrikam"]))).toEqual([]); // not in this file
  });

  it("says how many it adds, and how often each is there", () => {
    expect(addLabel(0)).toBe("Add words");
    expect(addLabel(1)).toBe("Add 1 word");
    expect(addLabel(5)).toBe("Add 5 words");
    expect(timesFound(1)).toBe("once");
    expect(timesFound(12)).toBe("12 times");
  });

  it("says when they don't fit in the list: a line each", () => {
    expect(noRoom(["Dana Lee", "Proxmox"], 17)).toBeNull(); // 8 + 1 + 7 + 1
    expect(noRoom(["Dana Lee", "Proxmox"], 16)).toMatch(/^These need 17 characters, .* room for 16\./);
    expect(noRoom([], 0)).toBeNull();
  });
});

describe("the box, once the saved list has changed under it", () => {
  it("holds the list as saved when nothing was being edited", () => {
    expect(carriedOver("", "", "Contoso")).toBe("Contoso"); // Settings has just loaded
    expect(carriedOver("Contoso\n", "Contoso", "Contoso\nProxmox")).toBe("Contoso\nProxmox");
  });

  it("holds the list as saved once the edits are the ones saved", () => {
    expect(carriedOver("Contoso\nDatto \n", "Contoso", "Contoso\nDatto")).toBe("Contoso\nDatto");
  });

  it("keeps edits that aren't saved yet, with what the list gained after them", () => {
    expect(carriedOver("Contoso: con toso\nDat", "Contoso", "Contoso\nProxmox\nDana Lee")).toBe(
      "Contoso: con toso\nDat\nProxmox\nDana Lee",
    );
    expect(carriedOver("Contoso\nProxmox", "Contoso", "Contoso\nProxmox\nDatto")).toBe("Contoso\nProxmox\nDatto");
    expect(carriedOver("", "Contoso", "Contoso\nProxmox")).toBe("Proxmox"); // the box was emptied
    expect(carriedOver("Datto", "Contoso", "Contoso")).toBe("Datto"); // the list gained nothing
  });
});
