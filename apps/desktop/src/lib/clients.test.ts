import { describe, expect, it } from "vitest";

import { clientHint, clientShown, followsOffered } from "./clients";
import { closing } from "./inplace";

describe("the client box", () => {
  it("opens on the client a meeting's people work for, so emptying it is no client", () => {
    const { name, dashed, start } = clientShown("Contoso", true, null);
    expect([name, dashed, start]).toEqual(["Contoso", true, "Contoso"]);
    expect(closing("Enter", start, "")).toEqual({ save: "", refocus: true });
    expect(closing("Enter", start, " Contoso ").save).toBeNull(); // left as it was: nothing saved
  });

  it("shows a name being saved as set, then the page's", () => {
    expect(clientShown("Contoso", true, "Fabrikam")).toEqual({ name: "Fabrikam", dashed: false, start: "Fabrikam" });
    expect(clientShown("Contoso", false, null).name).toBe("Contoso");
    expect(clientShown("Contoso", false, "").name).toBeNull(); // "no client" being saved
  });
});

describe("a meeting's client", () => {
  const kept = { client: "Contoso", client_inferred: false, client_kept: true, client_set: false };

  it("says when it was kept as it was, apart from one its people give it now", () => {
    expect(clientHint(kept)).toContain("kept");
    expect(clientHint({ ...kept, client_inferred: true, client_kept: false })).toContain("work for");
    expect(clientHint({ ...kept, client: null, client_kept: false, client_set: true })).toBe(
      "Set as not for a client: click to set one",
    );
  });

  it("can follow its people again once set or kept", () => {
    expect(followsOffered(kept)).toBe(true);
    expect(followsOffered({ ...kept, client: null, client_kept: false, client_set: true })).toBe(true);
    expect(followsOffered({ ...kept, client_inferred: true, client_kept: false })).toBe(false);
    expect(followsOffered({ ...kept, client: null, client_kept: false })).toBe(false);
  });
});
