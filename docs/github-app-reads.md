# Separate GitHub App budget for public reads

The optional worker-scoped `gh` shim authenticates recognized public reads using a
GitHub App **installation** token. Writes, `/user`, repository permission checks,
notifications, fork discovery, unknown commands and private repositories keep the
normal personal `gh` credentials. No global `gh auth` setting is changed. This
also covers recognized `gh` reads by host review/authoring subprocesses that inherit
the worker's PATH. Direct HTTP clients, absolute paths to `gh`, sandboxed agents,
and arbitrary GraphQL queries are not intercepted.

## Connect an App

From this checkout, run:

```sh
uv run python -m tauceti_worker.github_app_setup --fork ldct/TauCeti
```

Open the printed loopback URL. Review the private App registration on GitHub, then
install it with **Only select repositories → ldct/TauCeti**. The App requests only
read permissions for metadata, contents, issues, pull requests, checks, commit
statuses and Actions. Webhooks and user OAuth authorization are disabled. The
helper stores the private key locally, verifies the installation is restricted to
that public fork, and verifies it can read public upstream pull requests before
enabling the configuration. No credentials are printed or committed. The local
setup server exits on completion or after one hour.

For a remote VM behind a **private, authenticated HTTPS reverse proxy**, run the
helper from the source checkout with a fixed public origin, for example:

```sh
python -m tauceti_worker.github_app_setup --fork ldct/TauCeti \
  --port 8766 --public-origin https://tauceti.exe.xyz:8766
```

The listener remains loopback-only. The proxy must reach that listener and preserve
the public Host header. Callback URLs use the explicit origin; forwarded headers
are not trusted. Host and random state checks remain mandatory. Keep the proxy
private; never expose this credential-registration helper as a public service.
Resume a pending setup with the same fork, port, and public origin. The helper
prints a state-bearing setup link, but never private keys or installation tokens.

GitHub permits installed Apps to read public repositories outside their installation:
https://docs.github.com/en/apps/using-github-apps/installing-a-github-app-from-a-third-party

Registration uses the official manifest handshake:
https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest

## Configuration and runtime

Default configuration: `~/.config/tauceti/github-app.json`. Set
`TAUCETI_GITHUB_APP_CONFIG` to an absolute config path to override it, or `off` to
disable routing. The configuration has `app_id`, `installation_id`, `private_key`
(absolute path, owned by the current user, mode 0600), and `public_repositories`
(an explicit list of owner/repository names confirmed public by the operator).
Never add a private repository to this list.

Restart a worker after setup to activate its shim. New dashboard workers pick it
up automatically. The personal interactive shell remains unchanged. Tokens are
minted with read-only permissions, refreshed two minutes before expiry, and cached
under an interprocess file lock beside the config. Files are 0600 and the cache
directory is 0700. App errors do not fall back to personal authentication. Revoke
the installation or App key in GitHub to revoke its access.

Only approved REST endpoint patterns and explicit public `pr`/`issue` reads route
to the App. This includes public git refs/trees/blobs/commits and Actions job
metadata, supported inline/attached CLI flags, explicit `--hostname github.com`,
allowlisted representation/version headers, and audited query fields when the
method is explicitly GET. Fields without explicit GET remain personal because
`gh api` otherwise defaults to POST. Repository-root permission lookups and
notification endpoints remain personal.

The exact built-in survey/progress queries and TauCetiReview's public thread-ID
lookup also qualify. Arbitrary GraphQL, mutations, unsafe headers, unknown
repositories, implicit repository selection, and unsupported syntax retain
personal authentication. No installation permissions are broadened. Add another
repository to `public_repositories` only after verifying it is public; to offload
review rubric refs/trees, include `TauCetiProject/TauCetiReview`.

## Survey discussion-comment batching

Deep surveys prefetch only build-green, non-draft PRs whose scoreboard/marker
cache cannot answer at the observed `updatedAt`. Discussion (issue) comments are
read in batches of at most ten PRs before the existing parallel review checks.
Forty cold PRs with at most 100 comments each therefore require four comment
requests; unchanged cached PRs require none. Inline review/contest comments use the separate batching path below; all write
paths are unchanged.

The App router recognizes exactly ten built-in query documents (sizes 1–10),
with an explicitly allowed repository and PR numbers/cursors passed as variables.
Every comments connection is paginated with `first:100`, independently, up to
100 pages per PR. Errors, null/malformed data, inconsistent counts, cursor loops,
or the page cap discard that PR's entire fetched history as a failure, not an
empty or partial success. Unaffected aliases remain usable; failures retain the
existing stale-cache behavior and do not trigger a REST fallback.

Prefetched results belong only to the current `ReviewState` review phase, survive
slow prefetch/queued tasks without the short comment memo expiring, and are
cleared after the pool joins (including on errors). Forced dispatch reads always
bypass them. This adds no shared/cross-worker cache or review-pool policy changes.
Library/test GitHub adapters without a batch method retain the original per-PR
read path. Validation is offline: `uv run python tests/github_comments_batch.py`.

## Survey inline review-comment / contest batching

Within that same review-phase scope, inline comments are prefetched only for PRs
whose scoreboard says the current head was cleanly reviewed and whose contest
sidecar is not entitled to answer. Selection reads the discussion snapshot/cache
without pre-writing scoreboards or contest sidecars. Forty cold already-reviewed
PRs with small thread histories require four inline root requests (in addition to
four cold discussion requests), not forty REST requests. Warm unchanged contests
need no inline requests. Author-phase reads and forced dispatch reads retain their
existing behavior; force, bust, observe, and scope exit discard inline snapshots.

Ten exact root documents batch 1–10 PRs with `reviewThreads(first:10)` and nested
`comments(first:100)`. This avoids extra per-thread HTTP requests for ordinary
threads with up to 100 comments while keeping the root request bounded. Both connections are fully paginated. Each longer thread
uses one exact continuation document: repository → PR →
`reviewThreads(first:1,after:$threadCursor)` → `comments(first:100,after:$commentCursor)`.
The thread cursor is its predecessor edge cursor (omitted for the first thread).
Every continuation verifies PR number, thread ID, edge cursor and total counts.
There are no global `node(id:)` reads: even arbitrary cursor variables remain
bound to the allowlisted repository and PR. The App router requires the exact
document and allowed repository variables, rejecting extra fields/IDs.
Root requests select roughly 20,000 nodes including reply objects, below the
500,000-node limit; continuations are smaller. Request-count savings do not imply
a measured latency improvement; survey timing still needs a live comparison.

The adapter uses `fullDatabaseId` (BigInt), converting decimal strings directly
to Python integers without float rounding. It returns the existing REST-shaped
`id`, `body`, `in_reply_to_id`, and `created_at` fields. GraphQL's `replyTo` is a
parent relationship, whereas REST's reply endpoint only accepts top-level review
comments. Complete per-thread ancestry is therefore resolved to the unique root
before contest detection; the highest integer comment ID still wins, not a string
comparison or timestamp. Missing parents, cycles, duplicate/malformed IDs, null
nodes, inconsistent counts, and GraphQL errors discard the whole affected PR,
while healthy aliases survive. A failed fetch never writes a false no-contest
sidecar and never silently retries REST inside the review snapshot. Each connection
has a 100-page cap; reaching it means failure, not truncated success.

Schema/parent-semantics references (documentation only; no production API calls):
- https://docs.github.com/en/graphql/reference/objects#pullrequestreviewcomment
- https://docs.github.com/en/rest/pulls/comments#create-a-reply-for-a-review-comment

Offline coverage: `uv run python tests/github_review_comments_batch.py` includes
40-PR surveys, >100 threads and replies, IDs beyond 2^31 and 2^53, parent-chain
normalization, pagination failures, exact routing, cache selection, force/bust,
and review/author phase boundaries. No deployment or live API comparison is part
of these tests.

## Personal identity cache

CLI startup anchors `TAUCETI_GITHUB_IDENTITY_CACHE` to an absolute path before
worker HOME isolation (default `~/.cache/tauceti/github-identity/login.json`).
The shared, locked cache lasts 15 minutes and is keyed by a SHA-256 fingerprint of
the current personal credential and GitHub host. Credential rotation or account
switching invalidates it; a revoked token is not made usable for writes. Tokens
and profile responses are never persisted: only the fingerprint, public login,
and timestamp. Directory mode is 0700, files 0600; unsafe cache paths are refused.

The Worker and exact `gh api user --jq .login` / `gh api /user --jq .login`
queries through its shim reuse this cache. Misses always use the real CLI with
personal authentication. Failures and malformed identities are never cached;
cache failure falls back to the personal lookup. Non-github.com hosts bypass this
cache. Set the variable to `off` to disable persistent caching. Enabling it does
not inject GitHub credentials into the review engine's tokenless model environment.

## Bounded REST post-review notification muting

After a successful review, the Worker searches up to three pages of 100 repository
notifications (`all=true`, including read notifications), stopping when it finds
that PR's thread. It then makes one REST subscription PUT with `ignored=true`.
Both lookup and write stay personal; this path makes no GraphQL calls. Read
requests retain normal transient retries, but the write is never retried.
Missing threads or threads outside the first 300 entries are logged as failures;
there is no unbounded history scan or GraphQL fallback.

The response must contain the boolean `ignored:true` before success is logged.
HTTP success with `ignored:false` is a failure, not evidence of a mute. This
REST fallback replaces GraphQL IGNORED updates after live tests returned
UNSUBSCRIBED instead. REST also returned ignored:false in live samples, so the
fallback is not a proven fix for GitHub's behavior. Muting remains best-effort:
failure never invalidates or replays an already-posted review.

The loop's personal-budget preflight remains enabled: it still needs personal
quota for contributions and non-routed calls. An exhausted personal budget must
reset before a full round can safely run. This change reduces future consumption;
it does not bypass a rate limit or automatically fail over when an App is exhausted.

GitHub's documented installation limits:
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#primary-rate-limit-for-github-app-installations

Validation: `uv run python tests/github_app.py`, `./tests/run-all`, and the pinned
Ruff checks in CI. Tests use a disposable RSA key and fake `gh`; they never contact
GitHub, launch agents, or publish contributions.
