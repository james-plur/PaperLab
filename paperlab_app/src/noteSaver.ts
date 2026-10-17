/** Serialize saves and retain the latest edit while a request is in flight. */
export class NoteSaver {
  private content: string;
  private saved: string;
  private version: number;
  private running?: Promise<void>;
  constructor(content: string, version: number,
    private write: (content: string, version: number) => Promise<{version: number}>) {
    this.content = this.saved = content;
    this.version = version;
  }
  update(content: string) { this.content = content; }
  get dirty() { return this.content !== this.saved || !!this.running; }
  flush(): Promise<void> {
    if (this.running) return this.running;
    this.running = this.drain().finally(() => { this.running = undefined; });
    return this.running;
  }
  private async drain() {
    while (this.content !== this.saved) {
      const content = this.content;
      const result = await this.write(content, this.version);
      this.version = result.version;
      this.saved = content;
    }
  }
}
