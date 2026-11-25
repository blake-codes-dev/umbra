# Fortnite: scope, and what of Lyphen carries over

Queued second, and deliberately not started: the first product has to run one chunk inside Roblox
before the shape of a second one is worth guessing at. This page exists so the shared parts are
identified while they are still cheap to keep shared.

## The hard part is not the menu

Roblox gives us a Luau VM we can drive from outside. Fortnite is Unreal, and EasyAntiCheat is the
gate - a mod menu that only draws is not the product, a mod menu that survives EAC is. Three
realistic levels of ambition:

1. **External overlay, no injection.** Read what the game exposes (log tailing, window and process
   introspection), draw over the window with our own transparent overlay. No AC surface at all
   because nothing enters the process. Cheap to ship, and honest about it: no memory writes means
   no aim assist on real data, no engine hooks.
2. **Injected DLL, EAC-handled.** Needs either a driver or the documented bypass for the specific
   EAC build, plus our own manual mapper (which Lyphen already has). Deepest access, most moving
   parts, most breakage per patch.
3. **Overlay + a small injected reader.** Order-1 UI and comfort with a read-only process handle for
   the data the overlay shows. The pragmatic middle for a first release.

## What carries over unchanged

- The WPF shell, theme, animation and modal behaviour, and the toast/log surface - `src/app/Lyphen`
  is a product shell, not a Roblox shell.
- `Services/Injector.cs`: manual map, relocations, import binding, APC install. Only the target
  process name and the "which thread is safe" rule change.
- `tools/release.sh`, the hash post to `#builds`, the Discord panels and ticket flow.
- The spoofer's device side: the same disk serials, MACs and machine GUID matter for a hardware-ban
  surface, and Fortnite leans on hardware identifiers at least as hard as Roblox.

## What does not

- The Luau layer. Fortnite has no script VM to ride, so the menu is C++ over Unreal's own objects
  (UWorld, UGameInstance) instead of a Luau library the user can edit. Ship the menu as compiled
  code, keep a data file for offsets per patch so a patch is a config change, not a rebuild.
- The pattern/calibration approach still applies, but against UE object layouts and a much larger
  binary, so the anchor-file model (`anchors.txt` keyed by build) is the part to generalise first.

## First milestone, when it starts

Attach nothing. Get the overlay drawing over a live match with FPS and a clock, on the same window
tracking the Lyphen UI already does. That proves the shell, the transparency path and the "does not
get you banned" claim before any injection question is opened.
