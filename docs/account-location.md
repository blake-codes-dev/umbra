# Account location — what is real, what is a local illusion

Question from the owner: can the executor or the mod menu change the account's location?
Short answer: **the menu cannot, the spoofer can — but only through the same endpoint the
client itself calls, and Roblox re-detects it from device signals over time.** This file
records what was measured before anything was built on top of it.

## Facts (measured, this machine, 2026-11-24)

| Claim | Status | Evidence |
|---|---|---|
| Account location is server side and auto-detected from device signals and ip | measured (docs) | Roblox support article *Account Location*: “detected automatically … including your IP address”, “we do not collect precise geolocation”, updated over time |
| There is no location control inside the game client or in Luau | measured | `game`, `Players`, `LocalPlayer` expose no country; only `game:GetService("LocalizationService")` for language |
| A real write endpoint exists and is what the app calls | measured | `POST accountsettings.roblox.com/v1/account/settings/account-country` with `{targetCountryId}` → `401 Authentication token is missing` unauthenticated, `403 XSRF token invalid` first (i.e. the CSRF handshake is live, the route is not a 404) |
| The read endpoint returns the current country **and its id** | measured (docs) | `GET` same path → `{ value: { countryName, subdivisionIso, localizedName, countryId } }` |
| Both need `.ROBLOSECURITY` plus `x-csrf-token` | measured | 401/403 pair above; the token arrives on the failed POST |
| The GDK client keeps its cookies on disk in plain json | measured earlier | `%LOCALAPPDATA%\RobloxPCGDK\LocalStorage\appStorage.json` → `AppInstallationId`, cookies, `rbx-storage` |
| Country ids are not published | measured | only `countryId` of the *current* country is returned; no list endpoint on that host (`/countries`, `/options`, `/select` all 404) |
| Regional pricing is decided by **economic** location, not account location | measured (docs) | Roblox docs: economic location weighs VPN usage, billing history, account history; “users using a VPN … will be shown the default (highest) price for in-game items” |
| Support can move location only sometimes, and some regions are locked (VNG/Vietnam) | observed (devforum reports) | users report the option being refused or one-shot; Vietnam accounts cannot move at all |

## What the spoofer actually does

Card **Account location** in the spoofer window:

1. reads the cookie out of the client's own storage (no password, no login screen),
2. `GET` the current country and shows `countryName · id`,
3. `POST` the picked `targetCountryId`,
4. reads the account **back** and reports whether it really changed,
5. records every id→name pair it sees in `%LOCALAPPDATA%\Lyphen\location-ids.json`.

Step 5 is why a wrong seed id self-corrects: the table ships with common regions, and any
mismatch teaches the real pairing, so the second attempt on that country is right.

## Honest limits

- **Not a VPN.** Changing the account country does not move your ping, your datacenter, or
  which servers you match into. Matchmaking is ping-based and server-chosen.
- **It can revert.** Roblox keeps re-detecting; a mismatch between the account country and the
  device signals is exactly the case where it flips back on its own.
- **Economic location is a different thing.** Account location may not change the price you
  see. Billing country is set by the payment method.
- **One-way risk.** There is no “undo” endpoint; the spoofer reports what Roblox says and keeps
  a learned table rather than pretending a rollback exists.
- **Local only, no reboot needed.** This is an HTTP call, not a registry write, so it works
  without administrator — the spoofer's banner does not apply to this card.

## Ban risk

Lowest of everything the product does. It is the account's own settings API, the same call the
first-party client makes on sign-in. Nothing is injected and no invariant is broken. The realistic
worst case is “operation not permitted” (403), not a ban. The one thing worth saying out loud:
a country you cannot back up with payment or phone can make later support tickets about billing
more awkward.

## Not built (and why)

- Language/locale is a separate axis (`LocalizationService`, the launcher `-locale` flag). The
  menu can set the in-game language; the account location card deliberately does not touch it.
- Subdivision (state/province) is accepted by the API as `subdivisionIso` but is cosmetic.
- Moving the *billing* country needs a new payment method; out of scope for a client.

## Verification still owed

1. A live cookie run to confirm ids for the top ten regions (needs the alt account signed in).
2. Whether the GDK client writes its own country back on next launch (would explain a revert).
3. Whether a regional price actually changes after a location change — measure, do not claim.

Related: `docs/spoofer.md` for the device identity half, `PLAN.md` §5 for the executor scope.
