import { describe, expect, it } from "vitest";

import { kindShown, pickAfterSummary } from "./kinds";

describe("the kind of meeting a summary is written as", () => {
  const picked = { template: "standup", template_source: "auto" };

  it("shows a kind picked on the page, else the meeting's own, else the usual kind", () => {
    expect(kindShown("customer", picked, "auto")).toEqual({ value: "customer", automatic: false });
    expect(kindShown(null, picked, "auto")).toEqual({ value: "standup", automatic: true });
    expect(kindShown(null, { template: "project", template_source: null }, "auto")).toEqual({
      value: "project",
      automatic: false,
    });
    expect(kindShown(null, { template: null }, "auto")).toEqual({ value: "auto", automatic: false });
    expect(kindShown(null, {}, undefined)).toEqual({ value: "general", automatic: false });
  });

  it("shows the kind it was written as once a summary is done, unless another was picked meanwhile", () => {
    expect(pickAfterSummary("auto", "auto")).toBeNull();
    expect(pickAfterSummary("customer", "auto")).toBe("customer");
    expect(pickAfterSummary(null, null)).toBeNull();
  });
});
