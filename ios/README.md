# Routing Panel for iPhone — build spec

A native SwiftUI app that does everything the web panel (`scripts/panel.py`) does, with
**profiles**: one per panel instance (for example `default` and `bruno`), each with its own
sign-in, its own passkey and its own session, switchable from the top bar.

This folder is the hand-off from the Pi (where Swift cannot be compiled) to a Mac. The server
side is finished; the app is still to be written. Everything needed is here or in the code this
file points at.

| Here | What |
|---|---|
| `README.md` | this spec |
| `Fixtures/*.json` | sanitised sample responses for model decoding tests and SwiftUI previews |
| `Fixtures/attestation-vector.json` | test vector for the passkey enrolment parser |
| `tools/attestation_reference.py` | reference implementation of that parser, proven against the server |

Scope: iPhone only, iOS 17+, portrait and landscape. The web panel is not changed. The panel has
no route editor and no Funnel switch, so the app has none either.

## 1. How the server is addressed

A profile's **base URL** is the panel address without a trailing slash, e.g.
`https://<host>.<tailnet>.ts.net/default` or `https://<host>.<tailnet>.ts.net/bruno/default`.
Every endpoint below is appended to it (`<base>/api/projects`).

The **origin** of a profile is scheme + host (+ port), lowercased, no path:
`https://<host>.<tailnet>.ts.net`. The passkey endpoints take it as a parameter, and the passkey
domain (RP ID) is its hostname. Instances on one hostname share that domain but keep separate
passkeys: each has its own `passkeys.json` and its sign-in options list only its own credentials,
so the system sheet offers the right passkey for the active profile.

Rules that apply to every request:

- **Session**: send `Cookie: <name>=<value>`. The cookie comes from the `Set-Cookie` header of a
  successful sign-in (`routing_panel_<instance>=…; Path=…; Max-Age=2592000; HttpOnly; SameSite=Lax`).
  Keep name, value and expiry (now + Max-Age) per profile in the Keychain.
- **Every POST needs `X-Panel-CSRF: 1`** — including the sign-in POSTs made while signed out.
  Without it the server answers 403 `missing X-Panel-CSRF header`.
- JSON bodies with `Content-Type: application/json`. A POST with no parameters sends no body.
- Errors are `{"error": "<message>"}` with a 4xx/5xx status; show the message as it is.
- **401** on anything except the sign-in calls means the session is gone: delete that profile's
  stored cookie and show its sign-in screen.
- Do not send `X-Panel-Token`. The token is typed once to sign in and never stored.
- Use an ephemeral `URLSession` with `httpShouldSetCookies = false` and no cookie storage, and set
  the `Cookie` header by hand, so one profile's session can never be sent for another.
- Project names go into the URL percent-encoded as a single path segment.

## 2. Endpoints

Source of truth: `Handler._dispatch` and `Handler._auth_route` in `scripts/panel.py`. The web
page's own JavaScript (the `PAGE` string in the same file) is the reference client for behaviour.

### Sign-in and passkeys

| Call | Signed in? | Body → response |
|---|---|---|
| `GET api/auth/state?origin=<origin>` | no | → `{authRequired, authenticated, passkeysSupported, passkeysReason, passkeys}` (`passkeys` = how many this instance has for that origin) |
| `POST api/auth/login` | no | `{token}` → `{ok}` + `Set-Cookie`. 401 wrong token, 429 token use paused |
| `POST api/auth/passkey/options` | no | `{origin}` → `{challenge, rpId, timeout, userVerification, allowCredentials:[{type,id,transports?}]}`. 404 when none enrolled |
| `POST api/auth/passkey/verify` | no | `{id, clientDataJSON, authenticatorData, signature}` → `{ok}` + `Set-Cookie` |
| `GET api/auth/passkeys` | yes | → `{passkeys:[{id, name, origin, created, lastUsed}]}` (Unix seconds; `lastUsed` may be null) |
| `POST api/auth/passkeys/options` | yes | `{origin}` → `{challenge, rp:{id,name}, user:{id,name,displayName}, pubKeyCredParams, timeout, attestation, authenticatorSelection, excludeCredentials}` |
| `POST api/auth/passkeys/register` | yes | `{name, id, clientDataJSON, authenticatorData, publicKey, alg, transports}` → `{ok, name}` |
| `POST api/auth/passkeys/<id>/delete` | yes | → `{ok}` |
| `POST api/auth/logout` | yes | `{}` or `{everywhere: true}` → `{ok}` |

All binary values are base64url without padding. If `authRequired` is false the panel has no token:
every call works with no cookie, the other auth calls answer 400, and the app shows no sign-in or
Security screen for that profile.

### Projects and jobs

| Call | Body → response |
|---|---|
| `GET api/projects` | → array, see `Fixtures/projects.json` |
| `POST api/projects` | `{name, gitUrl}` (either may be empty) → `{ok, projectName, detail}`; work continues in the background |
| `GET api/create` | → `{creating, log}` |
| `POST api/projects/<name>/start` | → `{ok, pid}`; 409 if already running |
| `POST api/projects/<name>/stop` | → `{ok, signalled}` or `{ok, detail}` |
| `POST api/projects/<name>/update` | → `{ok, detail}`; stop, `git pull --rebase`, start — progress goes to the launch log |
| `POST api/projects/<name>/autostart` | `{launchOnStart: bool}` → `{ok, launchOnStart}` |
| `GET api/projects/<name>/log[?full=1]` | → **text/plain**: last 16 KB, or last 2 MB with `full` |
| `GET api/projects/<name>/env` | → see `Fixtures/env.json` (inline values, and only the key *names* of the .env file) |
| `POST api/projects/<name>/env` | `{env: {KEY: value}, envFile: "<path or empty>"}` — replaces the whole table → `{ok, …same as GET}` |
| `GET api/projects/<name>/claude` | → see `Fixtures/claude.json` (`screen` = terminal text, `attach` = SSH command) |
| `POST api/projects/<name>/claude` | `{action: "start" \| "stop" \| "trust"}` → `{ok, …}` |
| `POST api/apply`, `GET api/apply` | → `{ok, detail}`; `{applying, log}` |
| `POST api/redeploy`, `GET api/redeploy` | → `{ok, detail}`; `{redeploying, log}` — the panel restarts itself |

There is no streaming. The web page polls, and the app does the same: projects every 3 s; an open
log or Claude view every 1.5 s; apply / create every 0.8 s while running; redeploy every 1 s,
ignoring connection errors until the panel answers again. Stop all polling when the app leaves the
foreground or the view disappears.

### Meaning of the project fields (mirrors `updateCard` in the page)

- `state`: `running`, `stopped` or `partial`; `updating` true while Update & Restart runs.
- **Start** disabled when `!canLaunch`, or `state == running`, or busy. Reason: "no launchScript configured".
- **Stop** disabled when `state == stopped && !managed`, or busy.
- **Update & Restart** disabled when `!canUpdate`, or busy. Reason: "needs launchScript and appPath".
- **Start on boot** shows `launchOnStart`, disabled when `!canLaunch`.
- Busy = `updating`, or a request for that project is in flight.
- `ports`: the routes, read-only: path, port, and whether something is listening.
- `envCount`, `envFileProblem`: count and warning shown on the Environment row.
- `claude.state`: `off`, `starting`, `connected`, `waiting` (show "waiting for an answer"), `exited`.
  - **Start session** enabled when `available` and state is `off` or `exited`; otherwise show `reason`.
  - **End session** enabled unless state is `off`.
  - **Trust this folder** shown only when `trustFolder` is non-empty; confirm first, naming the folder.
  - **Open in Claude** shown when `sessionUrl` or `url` is non-empty; open `sessionUrl` if present,
    else `url`, with `UIApplication.open` — the Claude app claims the session link.

The web page shows these reasons as hover tooltips; the app shows them as footnote text.
Keep the confirmation wording of the page for trust, restart panel, remove passkey and sign out
everywhere (`confirm(` calls in `PAGE`).

## 3. Passkeys in a native app

The server needs no change. Three things make it work:

1. **Associated domain.** The hostname serves `/.well-known/apple-app-site-association` with
   `{"webcredentials":{"apps":["<TeamID>.<bundle id>"]}}` (generated by
   `scripts/generate-caddyfile.py` from `app-association.json` in the repo root on the Pi). The
   app carries the entitlement `webcredentials:<host>`. While developing, use
   `webcredentials:<host>?mode=developer` and turn on Settings → Developer → Associated Domains
   Development on the phone, so Apple's cache is bypassed.
2. **Sign-in.** `passkey/options` → `ASAuthorizationPlatformPublicKeyCredentialProvider(relyingPartyIdentifier: rpId)`
   `.createCredentialAssertionRequest(challenge:)` with the decoded challenge,
   `allowedCredentials` built from `allowCredentials[].id`, user verification required. Post the
   result to `passkey/verify`: `id` = `credentialID`, `clientDataJSON` = `rawClientDataJSON`,
   `authenticatorData` = `rawAuthenticatorData`, `signature` = `signature`.
3. **Enrolment.** `passkeys/options` → `createCredentialRegistrationRequest(challenge:name:userID:)`
   with `user.name` and the decoded `user.id`. iOS returns only `rawAttestationObject` (CBOR), but
   the server wants the raw authenticator data and the key as DER SubjectPublicKeyInfo. Port
   `parse_attestation` from `tools/attestation_reference.py` (a ~60-line CBOR reader plus a fixed
   26-byte P-256 header) as `AttestationParser`, and test it against
   `Fixtures/attestation-vector.json`. Then post `name`, `id`, `clientDataJSON`,
   `authenticatorData`, `publicKey`, `alg: -7`, `transports: ["internal", "hybrid"]`.

Passkeys already enrolled from Safari at the same address work in the app without re-enrolling.
A profile on a hostname that is not in the entitlement can still sign in with the token.

## 4. App structure

```
ios/
  project.yml                 XcodeGen project (the .xcodeproj is generated, not committed)
  Config.example.xcconfig     copy to Config.xcconfig (gitignored): DEVELOPMENT_TEAM,
                              PRODUCT_BUNDLE_IDENTIFIER, PANEL_HOST
  RoutingPanel/
    App/        RoutingPanelApp, AppModel (profiles + active session)
    Profiles/   Profile, ProfileStore (UserDefaults), KeychainStore, ProfileMenu, ProfileEditor
    API/        PanelClient, Models, PanelError
    Auth/       SignInView, PasskeyCoordinator, AttestationParser, Base64URL, SecurityView
    Features/   ProjectsView, ProjectDetailView, EnvEditorView, LogView, ClaudeSection,
                TerminalView, JobLogSheet, NewProjectSheet
    Support/    Poller
    RoutingPanel.entitlements  (webcredentials:$(PANEL_HOST))
  RoutingPanelTests/           AttestationParser, model decoding, Set-Cookie parsing
```

**Profiles.** `Profile { id, name, baseURL, tint }` in UserDefaults; the active one is remembered.
A menu in the top bar of the home screen lists the profiles with a checkmark and a "Manage
profiles…" entry (add, rename, change address, delete — deleting removes its Keychain item).
Adding a profile validates the address with `GET api/auth/state`. Switching replaces the active
`PanelClient` and its polling; a profile without a valid session shows its sign-in screen.
Session cookies live in the Keychain with `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`.

**Sign-in screen (per profile).** From `auth/state`: a passkey button when `passkeys > 0`, with the
token field underneath as the way back in; only the token field when there are none. After a token
sign-in, offer "Add a passkey for this profile". Show the server's message for 401 and 429.

**Screens.**

| Screen | Contents |
|---|---|
| Projects (home) | one row per project: name, state badge, routes; profile menu; **+** New project; menu with Apply routes, Restart panel, Security; pull to refresh |
| Project detail | Start / Stop / Update & Restart, Start on boot, routes, Environment, Launch log, Claude |
| Environment | editable key/value rows (add, swipe to delete), .env file path, keys found in the file, problems, Save |
| Launch log | wrapped monospaced text, follows the end, "Full log" switch, copy / share |
| Claude | state, Start / End session, Trust this folder, Open in Claude, terminal view, attach command with copy |
| New project | name and git URL, then the live create log |
| Apply routes / Restart panel | confirmation for restart, then a sheet with the live log |
| Security | passkeys with created and last-used dates, Add, Remove, Sign out, Sign out everywhere |
| Profiles | list, add, edit, delete |

**Sizing rules.**

- System text styles only, so Dynamic Type works up to the accessibility sizes; monospaced text
  uses a text style too. `@ScaledMetric` for status dots and icons.
- No fixed widths or heights. `List`, `Form` and `NavigationStack` handle safe areas and touch
  targets. Use `ViewThatFits` where a row of buttons or a label/value pair may not fit, falling
  back to a vertical stack.
- Logs wrap. The Claude terminal does not (tmux renders it 200 columns wide): scroll it in both
  directions.
- Check iPhone SE and Pro Max, portrait and landscape, light and dark, smallest and largest text.

## 5. Build

```sh
brew install xcodegen
cd ios
cp Config.example.xcconfig Config.xcconfig   # fill in team, bundle id, panel host
xcodegen
xcodebuild -scheme RoutingPanel -destination 'platform=iOS Simulator,name=iPhone 16' test
open RoutingPanel.xcodeproj
```

The bundle ID and Team ID must match the entry in the Pi's `app-association.json`.

## 6. Test checklist (real iPhone, once per profile)

- [ ] Add both profiles; switching shows each instance's own projects.
- [ ] Sign in with the passkey: the sheet offers only that instance's passkey.
- [ ] Sign in with the token on a profile without a passkey, then add a passkey from the app.
- [ ] Start, Stop, Update & Restart, Start on boot.
- [ ] Edit and save environment variables and the .env path.
- [ ] Launch log follows; Full log loads.
- [ ] Claude: start, terminal view updates, Open in Claude opens the conversation, end session.
- [ ] New project (empty and from a git URL).
- [ ] Apply routes; Restart panel (log continues after the panel comes back).
- [ ] Security: list, remove a passkey, sign out, sign out everywhere.
- [ ] Largest accessibility text size on the smallest phone: nothing clipped or overlapping.
