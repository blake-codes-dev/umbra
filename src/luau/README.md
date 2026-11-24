# Umbra menu

`menu.luau` is the in-game panel. Runs from the executor, nothing else needed.
RightShift toggles it, title bar drags, tabs are Player / Visuals / World / Scripts.

Layout choices that matter:
- pure instances, no Drawing for the panel itself, so it survives resolution changes
- ESP uses `Drawing` (boxes, names) because it has to sit over the 3D view
- config lives in `state`, and `unload()` disconnects everything and removes ESP

Features are deliberately small: walkspeed, jumppower, infinite jump, noclip, heal,
fullbright, boxes, names, fog, position copy, script runner with a console.
Nothing here needs a game-specific offset, so it works anywhere and breaks loudly
instead of silently.
