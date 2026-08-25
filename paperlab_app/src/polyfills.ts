// PDF.js 5.7 uses the ES2025 Uint8Array hex helpers. Electron 37's Chromium
// does not expose them yet, so install the small standards-compatible subset
// before the PDF.js module is evaluated.
type Uint8ArrayHexPrototype = {
  toHex?: () => string;
};

const uint8ArrayPrototype = Uint8Array.prototype as unknown as Uint8ArrayHexPrototype;

if (typeof uint8ArrayPrototype.toHex !== "function") {
  Object.defineProperty(Uint8Array.prototype, "toHex", {
    configurable: true,
    writable: true,
    value(this: Uint8Array): string {
      let result = "";
      for (const byte of this) result += byte.toString(16).padStart(2, "0");
      return result;
    },
  });
}

export function bytesToHex(bytes: Uint8Array): string {
  return (bytes as Uint8Array & {toHex: () => string}).toHex();
}
