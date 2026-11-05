# Mac runbook: build the iPhone app and ship it to TestFlight

This file is a brief for Claude Code running on the owner's Mac, in this repository. Follow it
from top to bottom. The owner starts you with: "Follow ios/MAC_RUNBOOK.md".

## The job

Build the native iPhone app for the routing panel and get a working build into TestFlight on the
owner's own Apple Developer account. The app must do everything the web panel does, with one
**profile** per panel instance (the owner has two on one hostname: `/default` and
`/bruno/default`), a separate passkey and session per profile, and layouts that adapt to every
iPhone size and text size.

**Read [`README.md`](README.md) in this folder first.** It is the spec: how the server is
addressed, every endpoint, the passkey flows, the screens and the sizing rules. This runbook only
adds the order of work, the checks, and the release steps. Where the two disagree, the spec wins
on behaviour and this file wins on process.

## Where things stand

Done, on the Pi that hosts the panel:

- The server side. No panel code needs to change for the app. `scripts/generate-caddyfile.py` can
  serve Apple's site-association file, but the Pi does not have its `app-association.json` yet
  because the app ID was not known. Phase 4 fixes that.
- `ios/README.md` (spec), `ios/Fixtures/*.json` (sanitised sample responses),
  `ios/tools/attestation_reference.py` (reference for the passkey enrolment parser, already proven
  against the server's own auth code) and `ios/Fixtures/attestation-vector.json` (its test vector).

Not done: every line of Swift, the Xcode project, and everything about signing and release. The
Pi has no Swift toolchain, so nothing here has been compiled. In particular, the `xcodebuild`,
`xcodegen` and `simctl` commands in this file were written from memory and never run: check
flags against the installed tools (`xcodebuild -help`, `xcodegen --help`, `xcrun simctl help`)
and adapt them rather than trusting them.

## Standing rules

- Never write the panel's setup token, an App Store Connect API key or a password into any file,
  commit or command line. The owner types the token into the running app. You do not need it.
- `ios/Config.xcconfig` and `ios/*.xcodeproj/` are gitignored. Keep it that way. The real
  hostname and Team ID belong only in `Config.xcconfig`; committed files use placeholders.
- Some steps only the owner can do (Apple account, App Store Connect website, anything on the
  physical iPhone). They are marked **Owner**. Stop, say exactly what to do, and wait.
- Finish each phase's check before starting the next. If a check cannot be run, say so plainly
  instead of reporting the phase as done.
- Commit after each phase that passes its check, on the current branch, with a message that says
  what now works. Push only when the owner says to.
- Do not change files outside `ios/` except `.gitignore` and the top-level `README.md`, and do not
  change the panel's Python code. If the server seems to need a change, stop and explain why.
- Use only Apple frameworks: SwiftUI, Foundation, AuthenticationServices, Security. No CocoaPods,
  no third-party packages.

## Phase 0 — What is needed before starting

Check the tools, and ask the owner for anything missing in one go:

```sh
xcodebuild -version          # Xcode 16 or later
xcodegen --version           # if missing: brew install xcodegen
xcrun simctl list devices available | head -40
```

**Owner** provides:

1. Xcode signed in to the Apple account that holds the paid developer membership
   (Xcode → Settings → Accounts).
2. The **Team ID**: 10 characters, shown at developer.apple.com/account under Membership details.
3. The **bundle ID** to use, e.g. `com.<name>.RoutingPanel`. It cannot be changed after the first
   upload.
4. The panel **hostname**, without `https://` and without a path
   (the part before `/default` in the panel's address).
5. The app's name as it should appear under the icon.

Then create `ios/Config.xcconfig` (and commit a `Config.example.xcconfig` with placeholders):

```
DEVELOPMENT_TEAM = <Team ID>
PRODUCT_BUNDLE_IDENTIFIER = <bundle ID>
PANEL_HOST = <hostname>
```

Check: the three values are set, and `git status` does not list `Config.xcconfig`.

## Phase 1 — Project skeleton

Create, under `ios/`:

- `project.yml` for XcodeGen: one iOS application target `RoutingPanel`, deployment target iOS
  17.0, iPhone only (`TARGETED_DEVICE_FAMILY = 1`), automatic signing, `Config.xcconfig` applied
  to both configurations, generated Info.plist with a launch screen, portrait and landscape.
- `PanelKit/` — a local Swift package (platforms iOS 17 and macOS 14) holding everything that is
  not a screen, so it can be tested on the Mac with plain `swift test`. The app target depends on
  it.
- `RoutingPanel/RoutingPanel.entitlements` with the associated domain
  `webcredentials:$(PANEL_HOST)$(ASSOCIATED_DOMAIN_SUFFIX)`. Set `ASSOCIATED_DOMAIN_SUFFIX` to
  `?mode=developer` for Debug and to nothing for Release. Developer mode makes the phone read
  the association file straight from the host instead of Apple's cache.
- A minimal `RoutingPanelApp` that shows a placeholder screen.

```sh
cd ios && xcodegen
xcodebuild -project RoutingPanel.xcodeproj -scheme RoutingPanel \
  -destination 'platform=iOS Simulator,name=<an available iPhone>' build
```

Check: the build succeeds and the placeholder app launches in the simulator.

## Phase 2 — Core (`PanelKit`)

Everything in spec §1–§3 that has no UI:

- **Models** for every response in spec §2, decoded from `ios/Fixtures/`. Decode leniently:
  unknown keys are ignored, and a missing optional field must not fail the whole list.
- **`PanelClient`**, one per profile: builds URLs from the profile's base URL, sends the stored
  cookie by hand, adds `X-Panel-CSRF: 1` to **every** POST (sign-in included), turns
  `{"error": …}` bodies into errors carrying the server's message, and reports a 401 so the app
  can drop the session. Ephemeral `URLSession`, no cookie storage. The launch log is plain text,
  not JSON.
- **`Set-Cookie` parsing** into name, value and expiry (now + `Max-Age`).
- **Session storage** behind a protocol, so tests use an in-memory store and the app uses the
  Keychain (this-device-only, after first unlock), keyed by profile id.
- **Profiles**: `Profile { id, name, baseURL, tint }`, stored in UserDefaults; the origin is
  derived from the base URL as spec §1 describes.
- **`Base64URL`** (no padding) and **`AttestationParser`**: a port of `cbor_read` and
  `parse_attestation` from `ios/tools/attestation_reference.py`.

Tests, in the package: every fixture decodes; `AttestationParser` turns
`attestation-vector.json`'s `attestationObject` into exactly the three `expected` values and
rejects truncated input; `Set-Cookie` parsing; URL building for a project name containing a space
and a slash; a POST built by the client carries the CSRF header. Read the fixtures from the
`ios/Fixtures` folder by path relative to the test file.

```sh
cd ios/PanelKit && swift test
```

Check: all tests pass.

## Phase 3 — Screens

Build the screens of spec §4 on top of `PanelKit`, following its sizing rules. Order that keeps
the app runnable at every step: profiles and the profile menu → sign-in with the token →
projects list → project detail and its actions → environment → launch log → Claude section →
new project → apply routes and restart panel → Security → passkey sign-in and enrolment.

Points that are easy to get wrong:

- The enable/disable rules and reason texts for each button are in spec §2 ("Meaning of the
  project fields"). The confirmation texts are the `confirm(` calls in the `PAGE` string of
  `scripts/panel.py`.
- Polling follows the intervals in spec §2 and stops when the scene is not active or the view is
  gone. Restart-panel polling must survive connection errors while the panel is down.
- Switching profile must cancel the old profile's polling and never show one profile's data
  under the other's name.
- Passkeys cannot be exercised in the simulator in any useful way; sign in there with the token.
  Write the passkey code now and verify it in phase 5.
- Add a **debug-only demo mode** (for example a launch argument) that feeds the screens from
  `ios/Fixtures/` with no server, so layouts can be checked and screenshotted without signing in.

Check, in the simulator, on the smallest and the largest iPhone available, portrait and
landscape, light and dark:

```sh
xcrun simctl ui booted content_size extra-small
xcrun simctl ui booted content_size accessibility-extra-extra-extra-large
xcrun simctl ui booted appearance dark
xcrun simctl io booted screenshot <file>.png
```

Look at the screenshots yourself. No text is clipped or overlapping, every control is reachable,
and long project names, paths and log lines wrap or scroll. Then, with the owner's go-ahead, add
a real profile in the simulator (the owner types the address and token) and confirm the projects
list and one harmless action against the live panel.

## Phase 4 — Tell the hostname about the app

Native passkeys only work once the panel's hostname names this app. The app ID is
`<Team ID>.<bundle ID>`. On the Pi, in the repository checkout that runs the **default** panel
(`~/projects/routing` for the user that owns it), `app-association.json` must contain:

```json
{
    "appIDs": ["<Team ID>.<bundle ID>"]
}
```

followed by `scripts/apply.sh` there (or the **Apply routes** button in the default panel). The
file is gitignored on purpose, so it cannot arrive by `git pull`.

**Owner** chooses how it gets there: run those two steps on the Pi themselves, give the app ID to
the Claude session on the Pi, or allow you to do it over SSH:

```sh
ssh <user>@<pi> 'cd ~/projects/routing && printf "{\n    \"appIDs\": [\"%s\"]\n}\n" "<Team ID>.<bundle ID>" > app-association.json && scripts/apply.sh'
```

This reloads the web server in front of every project on that host. It is a reload, not a
restart, but get the owner's explicit yes before running it.

Check:

```sh
curl -si https://<hostname>/.well-known/apple-app-site-association
```

returns `200`, `Content-Type: application/json` and
`{"webcredentials":{"apps":["<Team ID>.<bundle ID>"]}}`, with no redirect. The panel must still
answer at `https://<hostname>/default/`. Apple's own copy, which Release builds use, is at
`https://app-site-association.cdn-apple.com/a/v1/<hostname>`; it can lag by hours, and it only
exists while the address is reachable from the internet (Tailscale Funnel on).

## Phase 5 — On the iPhone

**Owner**: connect the iPhone, trust the Mac, and in Settings → Developer turn on
**Associated Domains Development** (Developer appears after the first run from Xcode; Developer
Mode itself is under Settings → Privacy & Security).

Run the Debug build on the phone from Xcode, or:

```sh
xcodebuild -project ios/RoutingPanel.xcodeproj -scheme RoutingPanel \
  -destination 'generic/platform=iOS' -allowProvisioningUpdates build
```

The first signed build registers the bundle ID with the Associated Domains capability. If signing
fails on that capability, the owner enables it for the identifier at
developer.apple.com/account/resources/identifiers.

Check: the owner works through spec §6 (the test checklist) for **both** profiles. The points
that matter most: each profile's passkey sheet offers only that instance's passkey; a passkey
added from the app appears in that panel's Security list and signs in afterwards; switching
profiles back and forth never asks for the wrong one. Fix what fails and repeat.

## Phase 6 — TestFlight

Prepare the build:

- **App icon**: one 1024×1024 PNG with no transparency in the asset catalog's `AppIcon`. Ask the
  owner for artwork; if they have none, generate a simple one and show it to them.
- **Info.plist values**: the display name from phase 0; `ITSAppUsesNonExemptEncryption = NO`
  (the app only uses the system's HTTPS and passkey crypto), which avoids the export-compliance
  question on every build.
- **Privacy manifest** `PrivacyInfo.xcprivacy` in the app target: no tracking, no collected data,
  and the UserDefaults required-reason entry (`NSPrivacyAccessedAPICategoryUserDefaults`, reason
  `CA92.1`).
- **Versions**: `MARKETING_VERSION = 1.0`; `CURRENT_PROJECT_VERSION` must go up with every upload.
- Confirm the Release entitlement is `webcredentials:<hostname>` with no `?mode=developer`, and
  that Apple's copy of the association file (phase 4) shows the app ID. Without it, passkeys fail
  in the TestFlight build even though they worked in Debug.

**Owner**, once: at appstoreconnect.apple.com → Apps → **+** → New App: platform iOS, a name
(must be unique on the App Store; it need not match the name under the icon), primary language,
the bundle ID from phase 0 (it is listed once phase 5 has registered it), any SKU. Accept any
pending agreements shown there.

Archive and upload:

```sh
xcodebuild -project ios/RoutingPanel.xcodeproj -scheme RoutingPanel -configuration Release \
  -destination 'generic/platform=iOS' -archivePath build/RoutingPanel.xcarchive \
  -allowProvisioningUpdates archive

xcodebuild -exportArchive -archivePath build/RoutingPanel.xcarchive \
  -exportOptionsPlist ios/ExportOptions.plist -exportPath build/export \
  -allowProvisioningUpdates
```

with an `ExportOptions.plist` of `method = app-store-connect`, `destination = upload`,
`signingStyle = automatic` and the team ID. Keep `build/` out of git. If the command-line upload
cannot authenticate, do not ask for the owner's password: either the owner uploads the archive
from Xcode (Window → Organizer → Distribute App → TestFlight & App Store), or they create an App
Store Connect API key and you pass it with `-authenticationKeyPath`, `-authenticationKeyID` and
`-authenticationKeyIssuerID`, the key file staying outside the repository.

**Owner**, after the upload: App Store Connect → the app → TestFlight. The build appears after
processing (often 10–30 minutes). Create an Internal Testing group, add themselves, and install
the build from the TestFlight app on the iPhone. Internal testers need no Apple review.

Check: the build shows as ready to test, installs from TestFlight, and on that installed build
the owner signs in to both profiles with their passkeys and runs one action in each.

## When you finish

Report to the owner, in plain words: what is in TestFlight (version and build number), which
checks you ran yourself, which the owner ran, anything that was skipped or did not pass, and how
to ship the next build (bump the build number, archive, upload). Update `ios/README.md` where the
app as built differs from the spec, and the "iPhone app" section of the top-level `README.md`
with the real build steps.

Things worth telling them: TestFlight builds expire after 90 days; a panel session lasts 30
days, after which the app asks for the passkey again; and a profile on a different hostname needs
that hostname added to the entitlement and its own association file before passkeys work there
(the token sign-in works regardless).
