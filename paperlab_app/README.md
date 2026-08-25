# PaperLab Desktop

macOS desktop workbench for the local PaperLab library. The Electron process
starts an authenticated Python sidecar on a random loopback port and reuses the
existing `topics.yaml`, SQLite database, PDFs, and reports.

```bash
npm install
npm run dev
```

Build the unsigned Intel macOS application and DMG:

```bash
npm run package:mac
```

Artifacts are written to `release/`. The first launch looks for
`~/tools/paperlab/topics.yaml`; if it is not present, PaperLab asks the user to
select a configuration file.
