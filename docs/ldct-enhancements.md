# Consolidated ldct enhancements

`ldct/enhancements` is the integration branch for this fork. Its initial commit
squashes the remaining fork enhancements onto upstream `9d4e773` (September 26,
2026). Older feature branches remain available as historical references.

Install this branch with:

```sh
uv tool install --force git+https://github.com/ldct/TauCetiWorker@ldct/enhancements
```

This replaces an existing `tauceti` tool installation. Finish/drain running worker
rounds before upgrading it.

## Included changes

- Public-read GitHub App authentication and local registration/installation flow,
  from `feat/github-app-public-reads` (`39f9869`). See
  [GitHub App reads](github-app-reads.md).
- All dashboard compatibility fixes from
  `fix/taucetiui-worker-fixes-2026-09-26` (`e2251eb`): configurable quota reserve,
  post-review notification muting, concurrent survey checks and freshness
  ownership, narrower paginated GraphQL queries and failure diagnostics,
  progress-history handling, verified Codex fallback access, and pinned local
  progress sources. This includes the implementations tracked in upstream PRs
  [207](https://github.com/TauCetiProject/TauCetiWorker/pull/207),
  [208](https://github.com/TauCetiProject/TauCetiWorker/pull/208),
  [209](https://github.com/TauCetiProject/TauCetiWorker/pull/209), and
  [210](https://github.com/TauCetiProject/TauCetiWorker/pull/210), with the later
  upstream integration adjustments retained.
- Worker environment-table TOML serialization and round-trip coverage from
  `fix-toml-table` (`fdfb35e`, upstream
  [PR 181](https://github.com/TauCetiProject/TauCetiWorker/pull/181)).

The other historical enhancement branches have already landed upstream, including
Docker deployment, worker environment configuration, shared build caches, macOS
isolation, authoring profiles, quota refresh, roadmap coordination and source
support, and prompt changes. They are inherited from the upstream base.

`feat/review-in-progress-label` is deliberately excluded: upstream
[PR 103](https://github.com/TauCetiProject/TauCetiWorker/pull/103) was closed as
superseded because TauCeti CI owns those labels. Reintroducing it would restore
the rejected worker-side label writes.

## Keeping the branch current

Use this branch for subsequent fork improvements and installations. Merge newer
upstream `main` into it as needed and resolve overlaps against the final upstream
behavior. Existing branches and PRs are not rewritten or closed by this initial
consolidation.
