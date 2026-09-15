# Changelog

## 0.1.0

- Replace run lifecycle chat messages with the existing typing indicator:
  `run_started`/`completed`/`failed`/`cancelled` no longer post text. Compact
  tool notices and the approval prompt remain the only rendered execution text.
- Opt-in `chat_approvals`: render the approval prompt with one-shot
  Approve/Deny inline buttons and expose the tap through `Channel.approval`.
  A tap resolves only against the host's live pending approval and its
  owning-chat policy; recognized-but-stale taps are consumed, never model input.
- Advertise `approvals` and require Nagents `>=0.10.0,<0.11`: the
  `ChannelApproval` contract and `Channel.approval` hook this connector uses
  first ship in the 0.10 core line.

- Advertise `send_files` and upload `ChannelSend.files` once each: JPEG/PNG as
  `sendPhoto`, everything else as `sendDocument`, with the text as a caption when
  it fits Telegram's 1024-unit limit and a separate message otherwise. Files are
  capped at 20 MiB each and three per send; empty, oversized, and reference-only
  attachments are rejected before HTTP, and multipart uploads are never retried.

- Advertise `fetch_attachment` and download referenced Telegram files on host
  request: one `getFile` plus one authenticated byte download, capped at
  Telegram's 20 MiB bot limit, accepting only issued `telegram:file:` references,
  refusing traversal paths, keeping redirects off, and sanitizing failures.
  Inbound events also populate `sent_at`, `sender_name`, `sender_username` and
  `conversation_type` for the host's formatted model context.

- Require Nagents `>=0.9.0,<0.10`: the outbound attachment and presentation-field
  additions this connector uses first ship in the 0.9 core line.

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
