# Security and deployment

This is a casual, single-process game for trusted players. Publishing the source
does not expose your server's environment variables. Running the server on the
public internet exposes game creation, AI use, and resource consumption to visitors.

## Credentials and logs

- Set `OPENAI_API_KEY` only in the server environment or a private secret manager.
  Never put a real key in Python, JavaScript, documentation, screenshots, or issues.
- `.env`, `.env.*` (except `.env.example`), `.envrc`, common private-key files,
  `logs/`, and `dist/` are ignored by Git. Ignore rules do not remove files already
  committed and can be bypassed with `git add -f`.
- WebSocket diagnostics include private hands and raw agent output. Simulation
  training logs include every player's hand and bottom cards. Keep these private;
  do not attach full logs to public bug reports. Redaction is defense in depth,
  not a guarantee that arbitrary free text contains no secrets.
- New WebSocket log files are owner-readable/writable (`0600`); newly created log
  directories use `0700`. Existing directories retain their permissions. Rotating
  logs are bounded per process, but files from previous server runs are retained.
- If a real credential is ever committed, revoke/rotate it first, then remove it
  from all Git history. Deleting it in a later commit is insufficient.

## Guest identities and saved games

Guest identity secrets and names live in browser localStorage. Treat the identity
as a bearer credential: it can list that guest's pending tables and recover their
seats. The server stores only its SHA-256 hash; HTTP requests use Authorization
headers, WebSocket hello messages carry the secret, and communication logs redact
it. Keep the existing CSP and serve over HTTPS. Clearing browser storage loses
automatic access; nicknames and room codes are not recovery credentials.

SQLite snapshots contain all private hands, buried cards, and per-room reconnect
tokens. New database files use `0600` and new data directories use `0700`.
Database files are ignored by Git. Keep the database and backups outside static
roots and public artifacts; no automatic database retention cleanup is currently
implemented. See [persistence setup and backups](docs/PERSISTENCE.md).

## Hosting

For local-only play, bind to loopback:

```sh
./run.sh --host 127.0.0.1 --port 8768
```

The default `0.0.0.0` binding allows LAN play. Before exposing it publicly, put an
authenticated HTTPS reverse proxy in front of **all HTTP and WebSocket routes**,
restrict accepted hostnames, and firewall direct access to the application port.
Use a single application worker. Forward WebSocket upgrades and preserve the
public Host/Origin. Trust forwarded client IP headers only from that proxy.

There is no application account system, admission password, or total API spending
cap. A room code admits new players before dealing; guest identities recover
existing seats, with legacy reconnect-token support. Neither is a service-wide
authorization mechanism. Same-origin checks
block cross-site browser requests but do not authenticate native clients or
protect against DNS rebinding to an unrestricted hostname.

To turn off paid agent calls and CPU-heavy search for web clients, start a fresh
server with:

```sh
LEVELUP_WEB_AI_STRATEGIES=rule_based ./run.sh --host 127.0.0.1 --port 8768
```

`LEVELUP_WEB_AI_STRATEGIES` is a comma-separated allowlist enforced on room creation
and strategy changes, including direct requests. Restored rooms fall back to the
default strategy if their saved strategy is no longer allowed. The default rule
strategy always remains available. If unset, all registered strategies remain available to the
server; the UI separately hides the basic/XGBoost comparison policies. This setting
does not restrict offline simulations. Restart to apply deployment configuration.

The application limits room creation and identity updates to a combined 10/minute
per client IP, WebSocket messages to 16 KiB and 20 JSON nesting levels,
and traffic to 10 messages/second with a burst
of 40. Malformed messages and ping messages count toward this limit. Room creation
and identity-update bodies are limited to 8 KiB/10 seconds. A player gets at most
one slow AI hint per turn, including reconnects. These controls reduce abuse; they do not replace proxy
connection limits, authentication, resource quotas, or API spending controls.

Use the pinned `requirements.txt` for the tested web dependencies. The optional
agent dependency is installed with `.[agent]`; audit that resolved environment too.
Recheck dependencies and secrets before each release. Enable GitHub secret scanning
and push protection where available. Review GitHub Actions changes before running
them with repository secrets.

## Reporting

Please report vulnerabilities privately through GitHub's private vulnerability
reporting feature if the repository owner has enabled it. Do not post credentials,
live reconnect tokens, private hands, or exploitable details in a public issue.
