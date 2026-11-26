import { describe, expect, it } from "vitest";

import { factAbout, factsOf, quoted, secondOf } from "./said";

describe("secondOf", () => {
  it("gives the second kept for that item", () => {
    expect(secondOf([12.5, null, 300], 0)).toBe(12.5);
    expect(secondOf([12.5, null, 300], 2)).toBe(300);
  });

  it("gives none where the summary kept none, or nothing usable", () => {
    expect(secondOf([12.5, null], 1)).toBeNull(); // not said: no moment
    expect(secondOf(undefined, 0)).toBeNull(); // a summary from before 0.6.0, or written on this PC
    expect(secondOf([4], 3)).toBeNull(); // fewer seconds than items
    expect(secondOf(["01:02", -1, Number.NaN], 0)).toBeNull();
    expect(secondOf(["01:02", -1, Number.NaN], 1)).toBeNull();
    expect(secondOf(["01:02", -1, Number.NaN], 2)).toBeNull();
  });
});

describe("factsOf", () => {
  it("keeps facts with whom, what and the value", () => {
    const fact = { about: "Dana Lee", name: "Role", value: "Network lead", said_at: 61, quote: "I lead the network" };
    expect(factsOf([fact])).toEqual([fact]);
  });

  it("leaves out what isn't one", () => {
    expect(factsOf(undefined)).toEqual([]);
    expect(factsOf("facts")).toEqual([]);
    expect(factsOf([null, 3, { about: "Dana Lee", name: "Role" }, { about: " ", name: "Role", value: "Lead" }])).toEqual([]);
  });
});

describe("factAbout", () => {
  it("names the client as such, and a person as written", () => {
    expect(factAbout("client")).toBe("The client");
    expect(factAbout(" Client ")).toBe("The client");
    expect(factAbout("Dana Lee")).toBe("Dana Lee");
  });
});

describe("quoted", () => {
  it("puts the words in straight quotes", () => {
    expect(quoted(" Friday night ")).toBe('"Friday night"');
  });
});
