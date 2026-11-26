import { describe, expect, it } from "vitest";

import {
  at,
  host,
  listed,
  meetingLine,
  menuOf,
  sendsLine,
  statusLine,
  waitLine,
  whyText,
  type MeetingZachCore,
  type ZachCoreStatus,
} from "./zachcore";

const NOW = new Date(2026, 10, 25, 15, 0);
const TODAY = new Date(2026, 10, 25, 14, 14).getTime() / 1000;

const STATUS: ZachCoreStatus = {
  on: true,
  address: "https://notes.example.com",
  space: "work",
  key: true,
  parts: { summaries: true, tasks: true, facts: true, transcripts: false },
  never: [],
  waiting: 0,
  why: null,
  taking_back: 0,
  taking_back_why: null,
  there: 0,
  elsewhere: [],
  last_sent: null,
};

const MEETING: MeetingZachCore = {
  there: false,
  place: null,
  sent_at: null,
  waiting: false,
  taking_back: false,
  can_take_back: false,
  taken: false,
  wanted: true,
  on: true,
  kept: null,
};

describe("ZachCore in words", () => {
  it("names the server, this PC for 127.0.0.1", () => {
    expect(host("https://notes.example.com/zc")).toBe("notes.example.com");
    expect(host("http://127.0.0.1:8787")).toBe("this PC, port 8787");
    expect(host(null)).toBe("");
  });

  it("says a time of today without its day, another day's with it", () => {
    expect(at(TODAY, NOW)).toMatch(/^2:14\s?PM$/);
    expect(at(new Date(2026, 10, 24, 9, 5).toISOString(), NOW)).toMatch(/^Nov 24, 9:05\s?AM$/);
  });

  it("lists names as people say them", () => {
    expect(listed(["summaries"])).toBe("summaries");
    expect(listed(["summaries", "tasks", "facts"])).toBe("summaries, tasks and facts");
  });

  it("says where it sends, and whether it's on", () => {
    expect(statusLine(STATUS)).toBe("On, work at notes.example.com");
    expect(statusLine({ ...STATUS, on: false, space: "home" })).toBe("Off, home at notes.example.com");
    expect(statusLine({ ...STATUS, address: null })).toBe("Not set up");
  });

  it("says when it last sent, how many wait and why", () => {
    expect(waitLine(STATUS, NOW)).toBe("Nothing sent yet.");
    const paused = { ...STATUS, last_sent: new Date(TODAY * 1000).toISOString(), waiting: 3, there: 7, why: { kind: "paused", until: TODAY } as const };
    expect(waitLine(paused, NOW)).toMatch(/^Last sent 2:14\s?PM\. 3 wait: paused in ZachCore\.$/);
    expect(waitLine({ ...STATUS, waiting: 1, why: { kind: "off" } }, NOW)).toBe("1 waits: sending is off.");
    expect(waitLine({ ...STATUS, there: 2 }, NOW)).toBe("2 meetings in ZachCore.");
    const allTakenBack = { ...STATUS, last_sent: new Date(TODAY * 1000).toISOString() };
    expect(waitLine(allTakenBack, NOW)).toMatch(/^Last sent 2:14\s?PM\. None in ZachCore now\.$/);
    const back = { ...STATUS, taking_back: 1, taking_back_why: { kind: "no_key", address: "https://old.example.com" } as const };
    expect(waitLine(back, NOW)).toBe("1 meeting to be taken back: no key is saved for old.example.com.");
  });

  it("says each reason plainly", () => {
    expect(whyText({ kind: "refused", status: 401 }, NOW)).toBe("ZachCore refused the key (401)");
    expect(whyText({ kind: "refused", status: 404 }, NOW)).toBe("ZachCore has no such space");
    expect(whyText({ kind: "full", until: TODAY }, NOW)).toMatch(/^ZachCore is out of writing until 2:14\s?PM$/);
    expect(whyText({ kind: "elsewhere", count: 2 }, NOW)).toBe("2 meetings went to another server or space before");
    expect(whyText(null, NOW)).toBeNull();
  });

  it("says what goes, and whose meetings never", () => {
    expect(sendsLine(STATUS)).toBe("Sends summaries, tasks and facts.");
    expect(sendsLine({ ...STATUS, never: ["Contoso", "Fabrikam"] })).toBe("Sends summaries, tasks and facts. Never Contoso and Fabrikam.");
    const none = { ...STATUS, parts: { summaries: false, tasks: false, facts: false, transcripts: false } };
    expect(sendsLine(none)).toBe("Sends nothing: every switch is off.");
  });

  it("says on a meeting whether it went, waits, or is taken back", () => {
    expect(meetingLine({ ...MEETING, there: true, sent_at: new Date(TODAY * 1000).toISOString() }, NOW)).toMatch(/^Sent to ZachCore 2:14\s?PM$/);
    expect(meetingLine({ ...MEETING, waiting: true })).toBe("Waits to go to ZachCore");
    expect(meetingLine({ ...MEETING, waiting: true, there: true })).toBe("Its changes wait to go to ZachCore");
    expect(meetingLine({ ...MEETING, taking_back: true, can_take_back: true })).toBe("Being taken back from ZachCore");
    expect(meetingLine({ ...MEETING, taking_back: true, place: { place: "work https://x.example", address: "https://x.example", space: "work" } })).toBe(
      "Still in ZachCore, and can't be taken back: no key is saved for x.example",
    );
    expect(meetingLine({ ...MEETING, taken: true })).toBe("Taken back from ZachCore");
    expect(meetingLine({ ...MEETING, kept: "Nothing of Contoso goes" })).toBe("Nothing of Contoso goes to ZachCore");
    expect(meetingLine({ ...MEETING, kept: "It is private" })).toBeNull();
    expect(meetingLine(MEETING)).toBeNull();
  });

  it("offers Send now while sending is on, and Take back for what went or waits", () => {
    expect(menuOf(MEETING)).toEqual({ send: true, takeBack: false });
    expect(menuOf({ ...MEETING, on: false, there: true })).toEqual({ send: false, takeBack: true });
    expect(menuOf({ ...MEETING, kept: "It is private", waiting: true })).toEqual({ send: false, takeBack: true });
    expect(menuOf({ ...MEETING, taking_back: true, there: true })).toEqual({ send: false, takeBack: false });
    expect(menuOf(null)).toEqual({ send: false, takeBack: false });
  });
});
