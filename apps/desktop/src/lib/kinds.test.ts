import { describe, expect, it } from "vitest";

import { kindShown, pickAfterSummary } from "./kinds";

describe("the kind of meeting a summary is written as", () => {
  const picked = { template: "standup", template_source: "auto" };

  it("shows a kind picked on the page, else the meeting's own, else the usual kind", () => {
    const shown = (value: string, automatic = false) => ({ value, automatic, unpicked: false });
    expect(kindShown("customer", picked, "auto")).toEqual(shown("customer"));
    expect(kindShown(null, picked, "auto")).toEqual(shown("standup", true));
    expect(kindShown(null, { template: "project", template_source: null }, "auto")).toEqual(shown("project"));
    expect(kindShown(null, { template: null }, "auto")).toEqual(shown("auto"));
    expect(kindShown(null, {}, undefined)).toEqual(shown("general"));
  });

  it("says so when no kind could be picked, until another is picked on the page", () => {
    const failed = { template: "auto", template_source: null };
    expect(kindShown(null, failed, "general")).toEqual({ value: "auto", automatic: false, unpicked: true });
    expect(kindShown("customer", failed, "general").unpicked).toBe(false);
  });

  it("shows the kind it was written as once a summary is done, unless another was picked meanwhile", () => {
    expect(pickAfterSummary("auto", "auto")).toBeNull();
    expect(pickAfterSummary("customer", "auto")).toBe("customer");
    expect(pickAfterSummary(null, null)).toBeNull();
  });
});
