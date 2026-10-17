import {describe, expect, it} from "vitest";
import {NoteSaver} from "./noteSaver";

describe("NoteSaver", () => {
  it("flushes an edit immediately on leaving", async () => {
    const writes: unknown[] = [];
    const saver = new NoteSaver("old", 3, async (content, version) => {
      writes.push([content, version]); return {version: version + 1};
    });
    saver.update("new");
    await saver.flush();
    expect(writes).toEqual([["new", 3]]);
    expect(saver.dirty).toBe(false);
  });
  it("serializes in-flight edits using the returned version", async () => {
    let release!: () => void;
    const writes: unknown[] = [];
    const saver = new NoteSaver("old", 0, async (content, version) => {
      writes.push([content, version]);
      if (version === 0) await new Promise<void>(resolve => { release = resolve; });
      return {version: version + 1};
    });
    saver.update("first");
    const pending = saver.flush();
    saver.update("last");
    expect(saver.flush()).toBe(pending);
    release();
    await pending;
    expect(writes).toEqual([["first", 0], ["last", 1]]);
  });
  it("retains failed edits for retry", async () => {
    let fail = true;
    const saver = new NoteSaver("old", 1, async () => {
      if (fail) throw new Error("offline");
      return {version: 2};
    });
    saver.update("new");
    await expect(saver.flush()).rejects.toThrow("offline");
    expect(saver.dirty).toBe(true);
    fail = false;
    await saver.flush();
    expect(saver.dirty).toBe(false);
  });
});
