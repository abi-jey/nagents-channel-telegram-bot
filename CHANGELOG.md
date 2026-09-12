# Changelog

## 0.1.0

- Callable `ChannelPlugin` discovery metadata, a flat secret-aware configuration
  schema, and private direct-token or environment-reference factory configuration.
- Pure `/sessions`, `/session`, and `/new` host-command parsing with Telegram
  entity/recipient validation and preserved reply/forward provenance.
- Bounded, session-owned typing keepalives with rate-limit cooldowns and shielded
  lifecycle cleanup. Session selection remains the standalone/web host's policy.
- Initial standalone connector for the Nagents 0.6 public Channel API, including
  core prereleases (`nagents>=0.6.0a1,<0.7`).
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
