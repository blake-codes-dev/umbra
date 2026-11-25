# Build and release — the prompts a human runs

The app now **compiles and publishes in this container** (`tools/release.sh`, .NET 9 installed to
`/tmp/dotnet`, WPF cross-target via `EnableWindowsTargeting`). What still needs Windows is running
the exe and any live client test.

```bash
bash tools/release.sh 0.1.0-dev      # build, obfuscate, single-file zip, hash
```

Everything below is the same job by hand, plus the parts that are Windows-only.

## 1. Build the app by hand (Windows)

Only needed if the container recipe breaks. Needs the .NET 9 SDK, elevated, **from the repo root**.

```powershell
cd src\app\Lyphen
dotnet restore
dotnet publish -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:EnableCompressionInSingleFile=true
```

Output: `src\app\Lyphen\bin\Release\net9.0-windows\win-x64\publish\Lyphen.exe`.
Self-contained so a user without .NET can run it; single-file so there is one artifact to hash.

If publish fails on `System.Management` in single-file mode, drop
`-p:IncludeNativeLibrariesForSelfExtract=true` first. If it fails on the `library\*.luau` copy
items, the folder moved — fix the `..\..\luau\library` glob in `Lyphen.csproj` rather than
deleting the items.

Note for anyone editing XAML: **event handlers are `public` on purpose.** BAML resolves them by
name at load, so a renamed private handler is a `MissingMethodException` at runtime. `obfuscar.xml`
keeps the window and services types unrenamed for the same reason.

## 2. Obfuscate

The app is C#. WPF rules out a full IL merge into one method-per-class shape, so:

```powershell
dotnet tool install --global Obfuscar.GlobalTool
obfuscar.console obfuscar.xml
```

`obfuscar.xml` (repo root, committed) is set to rename only private/internal members, keep the
WPF-visible types (`App`, `MainWindow`, `SpooferWindow` — XAML resolves them by name), and keep
`Settings`/`Identity` property names because they are serialised to json. Public surface stays
readable, which is what a buyer decompiles first anyway.

Then, for strings and the license check:

1. `dotnet tool install --global ConfuserEx.CLI` — its `constants` and `ctrl flow` protections
   are what actually raise the cost, and both survive WPF once exclusions are set for
   `*Window` and `*.Baml`.
2. Sign nothing (a certificate costs money and buys trust only after verification); publish the
   SHA-256 instead and keep the hash in `#builds` next to the version.

If a protected build misbehaves, the fast diagnostic is: run the **unprotected** publish from
step 1 against the same script. That separates “my obfuscation broke it” from “the build is
broken”, which is the difference between an hour and a day.

## 3. Hash and stage

```powershell
Get-FileHash .\Lyphen.exe -Algorithm SHA256 | Format-List
```

Record version + hash in `WORKLOG.md`, then post to `#builds` behind the role gate. Two builds
exist at any time: `stable` and `beta`. Beta is free and labelled unstable.

## 4. Elevation prompt (the one the app copies for you)

The spoofer needs admin for HKLM writes. Settings → Administrator and the spoofer banner both
hand the user this, already filled in:

```powershell
Start-Process -FilePath "C:\path\to\Lyphen.exe" -Verb RunAs
```

No persistent scheduled task, no service, no stored password — elevation is per launch and the
user can say no and still use everything read-only.

## 5. Verify before calling a build sellable

```powershell
python tools\footprint_audit.py --json          # baseline before
python tools\hijack.py --dll src\native\stub.dll
python tools\footprint_audit.py --diff          # must show no module, no thread, no window
```

Then the pen-test list in `PRODUCT.md` §8: license bypass, forged token, HWID mismatch, discord
role gate, download leak, payment abuse. Results go in `state/pentest.md`, pass or fail.

## 6. Release notes

One line per build, always: version, hash, what changed, what is known broken, what to do if it
crashes. The changelog is the trust artifact; a build with no notes reads as abandoned.
