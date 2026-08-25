# Production deployment

Set the domain, Argon2id password hash, and cloud safety flags in `.env`:

```dotenv
PAPERLAB_ADDRESS=https://paperlab.cloud
PAPERLAB_ADMIN_PASSWORD_HASH=...
PAPERLAB_LOCAL_TRUSTED_MODE=0
PAPERLAB_COOKIE_SECURE=1
PAPERLAB_SYNC_SERVER=1
```

Create the password hash with `paperlab-publish auth hash-password`. Create runtime directories and start with `sudo docker compose up -d --build`. Only Caddy publishes ports; PaperLab remains on the private network.

Provision a device on the server with `paperlab-publish --config topics.yaml devices create "MacBook"`.
Configure the returned one-time token locally as `PAPERLAB_DEVICE_TOKEN`, plus
`PAPERLAB_SYNC_URL` and a stable `PAPERLAB_DEVICE_ID`. Device creation and revocation are
CLI-only; the authenticated browser remains read-only.

Before first sync, migrate a consistent SQLite snapshot and artifact directories while both Web instances are stopped. Verify `PRAGMA integrity_check`, counts, and sample SHA-256 values. Run `deploy/backup.sh` daily; set a private rclone target in `PAPERLAB_BACKUP_REMOTE` for off-server backups.
