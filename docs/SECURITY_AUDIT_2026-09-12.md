# Pre-publication security audit — 2026-09-12

## Result and scope

No real API keys, access tokens, or private keys were found in the files examined.
This is a point-in-time audit, not a guarantee of absence or a penetration-test
certification. Publishing this source and operating an anonymous public game server
have different risks; see [SECURITY.md](../SECURITY.md).

Reviewed the working tree (tracked plus non-ignored untracked files), all reachable
local Git history (one commit, `82e81cc`, 39 unique blobs), ignored local diagnostics,
web routes, WebSocket identity/actions, client rendering, AI prompts/responses,
dependency versions, and package inclusion rules. No keys were tested against an API,
no real model calls were made, and no repository changes were pushed.

## Secret and dependency scans

- Pattern scan: 60 publishable files and 101 ignored text/gzip files at the initial
  snapshot, plus Git history. Matches were only the documentation's `your-api-key`
  placeholder and a clearly fake mocked-SDK test credential.
- `detect-secrets`: additional findings were SHA-256 model/dataset fingerprints
  and the room-code alphabet. These are not credentials.
- `pip-audit`: no known vulnerabilities reported for the 27 installed runtime and
  OpenAI SDK dependency packages. Key versions: FastAPI 0.141.1, Starlette 1.6.0,
  Uvicorn 0.52.4, OpenAI 2.54.0. This does not cover every possible allowed dependency
  resolution or all optional training/build tools.
- Bandit: no high-severity findings. Its medium finding is the deliberate default
  all-interface LAN binding. Low findings concern seeded game/simulation randomness,
  a feature-schema assertion, and best-effort disconnect handling; these do not
  implement authentication. Identity tokens use `secrets.token_urlsafe(32)`.
- Existing logs are ignored, and static serving is confined to `static/`. The API
  key comes from server-side environment configuration and is absent from the model
  prompt, browser state, and normal error reports. Browser renderers use text nodes
  for player names, explanations, reactions, and errors, rather than HTML insertion.

## Findings fixed

| Severity | Finding | Change |
| --- | --- | --- |
| Medium | Secret files could accidentally enter a future commit | Ignore environment files and common private-key/credential files. |
| Medium | Diagnostic redaction missed common API-key fields; log permissions relied on umask | Normalize secret field names; redact recognizable OpenAI/GitHub tokens in free text; create/rotate logs with mode 0600. |
| Medium | Repeated slow hints could repeatedly start paid requests or search jobs for an unchanged move | Allow one slow hint per seat/turn, retained across reconnects. |
| Medium | Ping/invalid-message floods bypassed action throttling; alternate ASGI launches could lack the CLI's frame-size limit | Enforce message budget, size, and JSON nesting before payload logging/processing; cap room creation rate and HTTP body size/read time. |
| Low | Malformed origins, binary frames, and non-ASCII identity tokens could raise uncaught exceptions | Fail closed with controlled HTTP/WebSocket rejection. |
| Low | Missing browser security headers | Add CSP, frame denial, MIME-sniffing prevention, and no-referrer policy. |
| Medium | Dependency ranges permitted older vulnerable web frameworks | Raise web dependency minimums to the audited versions and require Starlette >=1.6. |

## Remaining deployment risks

1. **High on an anonymous internet deployment:** visitors can create their own rooms
   and consume the server owner's paid API budget when OpenAI Agent is available.
   The audit adds a server-enforced `LEVELUP_WEB_AI_STRATEGIES` allowlist. An
   authenticated gateway and spending controls remain necessary when offering paid AI.
2. **Medium on public deployment:** no global connection/CPU/API quota; distributed
   clients can work around per-IP/per-connection limits. Search can consume CPU, and
   long-lived connections or logs from repeated restarts can consume resources.
3. **Medium without HTTPS/host restrictions:** plain LAN HTTP exposes seat tokens to
   network observers. Matching Origin to a caller-supplied Host is not protection
   against DNS rebinding. Enforce TLS, trusted hostnames, and proxy authentication.
4. Diagnostic and training data are intentionally private and may contain complete
   hands. Do not publish them or old distribution artifacts by forcing ignored files
   into Git. Redaction cannot recognize every possible arbitrary secret string.

## Verification

96 targeted Python tests and 26 client tests passed, covering malformed origins/tokens/frames, flood and
size limits, log redaction/rotation permissions, strategy allowlist enforcement,
duplicate hints, private player views, reconnects, and existing agent/search behavior.
The agent tests use a mocked SDK transport. Built the wheel and inspected its 37
entries: only Python modules, model JSON, static assets, and distribution metadata;
no logs, environment files, private keys, or Git data. A real browser check confirmed
the CSP permits assets and WebSocket connection, the server allowlist leaves only
the rule policy visible, and an HTML-shaped player name renders literally. No
browser security-header or JavaScript errors were reported.

Review guidance: [OWASP WebSocket Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html).
OWASP recommends per-message authorization, origin validation, transport encryption,
and resource limits; the deployment risks above remain relevant after the local fixes.
