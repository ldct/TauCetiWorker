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

## Constant-cost post-review notification muting

After a successful review, the Worker obtains the public PR node ID and makes
one personal GraphQL `updateSubscription` mutation with state `IGNORED`. This
replaces the former unbounded, paginated traversal of the user's entire repository
notification history. The read is App-eligible; the mutation always stays personal,
is not automatically retried, and must explicitly confirm the same PR's ignored
subscription. Muting remains best-effort: failure never invalidates an already
posted review or skips archive processing.

The loop's personal-budget preflight remains enabled: it still needs personal
quota for contributions and non-routed calls. An exhausted personal budget must
reset before a full round can safely run. This change reduces future consumption;
it does not bypass a rate limit or automatically fail over when an App is exhausted.

GitHub's documented installation limits:
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#primary-rate-limit-for-github-app-installations

Validation: `uv run python tests/github_app.py`, `./tests/run-all`, and the pinned
Ruff checks in CI. Tests use a disposable RSA key and fake `gh`; they never contact
GitHub, launch agents, or publish contributions.
