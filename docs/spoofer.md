# Spoofer — device identity, and the elevation boundary

A separate window, and a separate product. It changes the identity a Roblox account leaves
behind on a machine, so a ban that is already recorded stops following you and a new account
does not inherit the old fingerprint blindly.

## What it touches

| Field | Where | Needs admin | Roblox reads it |
|---|---|---|---|
| machine guid | `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid` | yes | yes (native `UuidCreateSequential`/registry path) |
| product id | `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProductId` | yes | yes, via the same enumeration clients use for “device id” |
| computer name | `Win32_ComputerSystem.Rename` | yes | yes, plain `GetComputerName` |
| mac address | `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e972-…}\<n>\NetworkAddress` | yes | yes, `GetAdaptersAddresses` |
| volume serial, system uuid, baseboard/bios serial, cpu id, disk serial | read only | — | some, but only reliably writable with a reboot or vendor tool |

Deliberately not touched: SMBIOS (needs a flash utility), GPU/driver ids (break shader caches),
`InstallDate` (breaks activation checks), timezone (changes behaviour), and anything under
`HKLM\…\NetworkList` (VMs list, harmless but noisy).

## Boundary

- Reading always works, `SystemInfo` is pure WMI/registry and safe.
- Writing requires administrator, so the window shows a banner, a `Start-Process -Verb RunAs`
  relaunch, and a copyable prompt for users who would rather run it themselves.
- Every apply **backs up the originals first** to `%LOCALAPPDATA%\Lyphen\identity-backup.json`
  (written once, never overwritten by a later apply) and `Restore original` writes them back.
- Per-field results are reported. A field that fails says so; nothing is claimed as done that
  is not.
- Profiles (`%LOCALAPPDATA%\Lyphen\profiles.json`) are named identity sets — one per account or
  per store — and loading one fills the fields without writing.

## Hazards to keep in the README

1. Machine guid change makes Windows re-evaluate some licensing; Office and a few drivers can
   re-activate. Restore puts it back.
2. MAC change takes effect when the adapter is cycled. A disabled/enabled adapter in Device
   Manager is the fast path, a reboot is the reliable one.
3. Renaming the computer needs a reboot and, on a domain machine, can upset the join.
4. Nothing here is a proxy: geo-ip stays the same. See `account-location.md` for that half.

## Ban risk

- Spoofing device identity is normal practice and not itself detectable from the server side —
  Roblox sees a different machine, which is the intent.
- The risk is *coherence*: a machine guid from one build, a MAC from another vendor and a
  computer name matching neither is a fingerprint that looks synthetic if anyone correlates it.
  Profiles keep sets consistent on purpose.
- Reverting mid-session while in a game is fine; only the next handshake re-reads values.

## Verification owed

1. Apply on the alt, restart the client, dump `appStorage.json` and the observer snapshot, and
   check which fields Roblox actually echoes.
2. Confirm the restore path byte-for-byte against the backup.
3. Measure whether a ban on the alt stops following after a full spoof (this is the only claim
   that matters to a buyer, and it needs a real ban).
