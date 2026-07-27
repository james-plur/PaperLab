# PaperLab Local

PaperLab Local runs on macOS. It owns collection, offline storage, PDF download,
report generation, the trusted-loopback UI, and the incremental sync client.

It is an independently installable project and must not import
`paperlab_publish`.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[sync]'
cp topics.example.yaml topics.yaml
.venv/bin/paperlab-local web serve
```

Configure `PAPERLAB_SYNC_URL`, `PAPERLAB_DEVICE_ID`, and
`PAPERLAB_DEVICE_TOKEN` to synchronize with PaperLab Publish.
