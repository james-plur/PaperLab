import {describe, expect, it} from "vitest";
import {bytesToHex} from "./polyfills";

describe("Uint8Array hex compatibility", () => {
  it("encodes every byte with two lowercase hexadecimal digits", () => {
    expect(bytesToHex(new Uint8Array([0, 1, 15, 16, 255]))).toBe("00010f10ff");
  });
});
