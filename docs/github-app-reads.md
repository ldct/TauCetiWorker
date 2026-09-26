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
to the App. Two exact built-in GraphQL survey/progress queries also qualify; all
other GraphQL documents keep personal authentication. Unsupported CLI flag forms
conservatively keep personal authentication. Writes never use the App token.

The loop's personal-budget preflight remains enabled: it still needs personal
quota for contributions and non-routed calls. An exhausted personal budget must
reset before a full round can safely run. This change reduces future consumption;
it does not bypass a rate limit or automatically fail over when an App is exhausted.

GitHub's documented installation limits:
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#primary-rate-limit-for-github-app-installations

Validation: `uv run python tests/github_app.py`, `./tests/run-all`, and the pinned
Ruff checks in CI. Tests use a disposable RSA key and fake `gh`; they never contact
GitHub, launch agents, or publish contributions.
