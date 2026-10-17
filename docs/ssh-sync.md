# SSH synchronization

PaperLab 0.5.0 uses SSH as the default synchronization transport. The desktop
sidecar and `paperlab-local sync now` use the same implementation. HTTPS is an
explicit alternative; SSH errors never silently fall back to the public gateway.
The existing device-token protocol and incremental change journal remain in use.

## Configure a device

Configure an SSH host alias with your server address, login and identity file in
`~/.ssh/config`. Verify it once from a terminal:

```sh
ssh -o UserKnownHostsFile=~/.ssh/known_hosts -o StrictHostKeyChecking=ask paperlab-server true
```

This establishes trust in the host key and verifies that the key or SSH agent can
authenticate. Automatic synchronization uses batch mode and strict host-key
checking: it cannot prompt for a password, unlock a key or approve a new host.
PaperLab uses `~/.ssh/known_hosts` explicitly so a global `/dev/null` override
cannot disable host verification. Set `PAPERLAB_SSH_KNOWN_HOSTS` to use a different
persistent trust file. The account must permit TCP forwarding to `127.0.0.1:8765` on the server. A dedicated
account and key can restrict forwarding to that service.

Add these settings to the `.env` beside your `topics.yaml`; retain your existing
device ID and token. Start a new installation from `paperlab_local/.env.example`.

```dotenv
PAPERLAB_SYNC_TRANSPORT=ssh
PAPERLAB_SSH_HOST=paperlab-server
PAPERLAB_SSH_REMOTE_PORT=8765
PAPERLAB_DEVICE_ID=your-stable-device-id
PAPERLAB_DEVICE_TOKEN=your-device-token
PAPERLAB_AUTO_SYNC=1
```

Do not put private keys, passwords or device tokens in Git. The SSH host is an
alias from the user's SSH configuration; no personal hostname is built into the
application. The remote port is the PaperLab backend, not a public login gateway.
Restart PaperLab after changing `.env`.

```sh
paperlab_local/.venv/bin/python -m paperlab_local --config topics.yaml sync now
```

## Connection and recovery behavior

Each sync opens a private tunnel on an available `127.0.0.1` port, forwards it to
the server's `127.0.0.1:8765`, and closes it afterwards. HTTP inside the tunnel is
protected by SSH and still requires the device token. HTTP proxies and `.netrc`
settings are ignored for this connection. No public HTTP port is needed.

Keepalives detect broken SSH connections. A failure leaves pending changes intact
and records the error in sync status. While the desktop sidecar is running, its
existing automatic task retries every 60 seconds with a new tunnel. Sleep or a
network change therefore recovers on a later attempt. The CLI reports failures
and exits; rerun the command to retry. Nothing runs after PaperLab quits.

Sync drains the pending queue in batches of at most 500 changes and 4 MiB, instead
of stopping after the first 500. Concurrent local sync attempts are rejected with
an explicit message. File transfers retain the existing resumable upload protocol.
Successful acknowledgments survive a partially rejected batch.

## Explicit HTTPS alternative

```dotenv
PAPERLAB_SYNC_TRANSPORT=https
PAPERLAB_SYNC_URL=https://your-server.example
```

The URL must reach PaperLab's sync endpoints and support GET, HEAD, PUT and POST.
HTTPS certificate verification remains enabled. Plain HTTP is accepted only for
loopback development. SSH settings are not used in this mode.

## Diagnosing a failure

- **Host key verification failed:** verify the server identity and repair the
  appropriate `known_hosts` entry through SSH's normal workflow.
- **Permission denied:** check the SSH login, identity file, agent and key access.
- **Forwarding denied / connection refused:** confirm the account can forward TCP
  and PaperLab is listening on the configured server loopback port.
- **Device authentication failed:** check the PaperLab device token independently
  of the SSH key. Both authentication layers are required.
- **Another sync is already running:** allow that sync to finish, then retry.
