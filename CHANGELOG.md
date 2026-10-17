# Changelog

## 0.5.0

- Make managed SSH tunnels the default local sync transport. Preserve device-token
  authentication, strict host-key verification and a loopback-only local listener.
- Recreate tunnels on automatic retry, close them after sync and during shutdown,
  record failures, and retain explicit HTTPS support.
- Drain all pending changes in bounded batches and preserve accepted changes when
  a batch is partially rejected.
- Prevent equal values from generating field conflicts and preserve each change's
  source creation time separately from its local application time.
- Fix PDF removal propagation and concurrent PDF conflict handling.
- Serialize desktop note saves, flush before leaving or closing, and protect
  note files against stale writes and failed database updates.
- Align desktop, Python package and handshake application versions at 0.5.0.
  The compatible wire protocol remains 1.1.
