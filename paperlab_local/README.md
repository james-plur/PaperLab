# PaperLab Local

PaperLab Local is the Python sidecar for PaperLab.app. It owns collection,
offline storage, PDF download, abstract-based Chinese summaries, Markdown reading notes,
and incremental sync. It has
no browser UI and is not a user-facing local entry point.

It is an independently installable project and must not import
`paperlab_publish`.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[sync]'
cp topics.example.yaml topics.yaml
```

SSH is the default sync transport. Configure `PAPERLAB_SSH_HOST`,
`PAPERLAB_DEVICE_ID`, and `PAPERLAB_DEVICE_TOKEN` in the `.env` beside your
`topics.yaml`. See [SSH sync configuration](../docs/ssh-sync.md) for host-key
setup, automatic retry and the explicit HTTPS alternative.

PaperLab.app can import papers from uploaded PDFs, public PDF URLs, or BibTeX.
Related-paper discovery uses OpenAlex and falls back to Semantic Scholar; set
`S2_API_KEY` for a dedicated Semantic Scholar quota.

The macOS desktop workbench lives in `../paperlab_app` and is the only supported
local interface. It launches this package as an authenticated loopback sidecar.
