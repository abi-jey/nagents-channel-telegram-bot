# Changelog

## 0.1.0

- Advertise `fetch_attachment` and download referenced Telegram files on host
  request: one `getFile` plus one authenticated byte download, capped at
  Telegram's 20 MiB bot limit, accepting only issued `telegram:file:` references,
  refusing traversal paths, keeping redirects off, and sanitizing failures.
  Inbound events also populate `sent_at`, `sender_name`, `sender_username` and
  `conversation_type` for the host's formatted model context.

- Require Nagents `>=0.8.0,<0.9`: the outbound attachment and presentation-field
  additions this connector uses first ship in the 0.8 core line.

- Keep long polling alive across transient outages: when one `getUpdates`
  exhausts the transport's per-request retries, the listener backs off from one
  to 60 seconds and polls again, resetting after a successful batch. Permanent
  failures (401, 403, 409) still stop polling so the host can report them.

- Opt-in shared `Channel.on_event` execution rendering: compact safe tool previews,
  working/approval/terminal states, correlated stale-event rejection, bounded
  notices and existing four-second typing keepalives. Older SDK imports remain supported.
- Optional per-user admission by trusted numeric IDs or case-insensitive ASCII
  usernames, conjunctive chat filtering, and private-only human/chat identity
  validation. Filtering precedes host envelopes/commands and callback protocol
  acknowledgements; rejected callbacks are discarded without answering them.
- Callable `ChannelPlugin` discovery metadata, a flat secret-aware configuration
  schema, and private direct-token or environment-reference factory configuration.
- Pure `/sessions`, `/session`, and `/new` host-command parsing with Telegram
  entity/recipient validation and preserved reply/forward provenance.
- Bounded, session-owned typing keepalives with rate-limit cooldowns and shielded
  lifecycle cleanup. Session selection remains the standalone/web host's policy.
- Initial standalone connector for the Nagents public Channel API.
- Bounded Telegram long polling, durable-admission offset handling, callback
  protocol acknowledgements, optional chat admission filtering, and file references.
- Separate ingress update IDs, directly reusable transport `reply_to` IDs, and
  original reply provenance in `metadata.in_reply_to`.
- Explicit plain-text sends and schema-validated edit/delete actions with no
  uncertain outbound retries.
- Environment-based plugin factory, typed package, offline tests, cross-version
  CI and PyPI Trusted Publishing from pinned, tested commits: feature-commit alpha
  tags, future manual feature-ref alphas, and stable tags on reviewed main history.
- Draft PR milestones with scaffold-only main seeding and alpha-tag bootstrapping
  before the publishing workflows are merged.
