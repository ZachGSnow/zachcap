import { describe, expect, it } from "vitest";

import { closing, saveShowing, shownValue } from "./inplace";

describe("a box changing a value in place", () => {
  it("saves a change on Enter or a click elsewhere, never on Escape", () => {
    expect(closing("Enter", "", "Contoso").save).toBe("Contoso");
    expect(closing("blur", "INC-1234", " INC-5678 ").save).toBe("INC-5678");
    expect(closing("Escape", "Contoso", "Fabrikam").save).toBeNull();
    expect(closing("Enter", "INC-1234", "INC-1234 ").save).toBeNull(); // left as it was
  });

  it("gives focus back to its button after a key, not after a click elsewhere", () => {
    expect(closing("Enter", "", "Contoso").refocus).toBe(true);
    expect(closing("Escape", "", "").refocus).toBe(true);
    expect(closing("blur", "", "Contoso").refocus).toBe(false); // the click put it somewhere
  });

  it("shows a value only while it's being saved, so a refused one goes", async () => {
    const shown: (string | null)[] = [];
    await saveShowing("INC-5678", async () => false, (pending) => shown.push(pending));
    expect(shown).toEqual(["INC-5678", null]);
    expect(shownValue("INC-1234", "INC-5678")).toBe("INC-5678");
    expect(shownValue("INC-1234", null)).toBe("INC-1234");
    expect(shownValue("INC-1234", "")).toBeNull(); // taking it off
  });
});
