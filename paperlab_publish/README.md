# PaperLab Publish

PaperLab Publish is the independently deployable public web and sync service.
Anonymous users have read-only access; authenticated administrators can edit,
generate content, and resolve conflicts.

It must not import `paperlab_local`. Its only connection to a Local installation
is the versioned HTTPS protocol documented in `../protocol/`.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[backup]'
cp topics.example.yaml topics.yaml
.venv/bin/paperlab-publish web serve --host 127.0.0.1
```
