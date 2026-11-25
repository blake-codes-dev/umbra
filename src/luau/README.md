# Lyphen menu

`menu.luau` is the in-game panel. Runs from the executor, nothing else needed. RightShift toggles
it, the title bar drags, tabs are Player / Visuals / World / Scripts.

It ships with the scripts already in it, because a menu that starts empty is a menu nobody uses.
`library/*.luau` are those scripts, one file each, and `menu.luau` carries an inlined copy of them
generated at build time.

## Editing

- Never hand-edit between `-- [[LIBRARY-BEGIN]]` and `-- [[LIBRARY-END]]`. `tools/build_menu.py`
  rewrites that block from `library/`.
- Add a script by dropping a `.luau` file with a two-line header, then adding its name to `ORDER`
  in `tools/build_menu.py`. `python tools/build_menu.py --check` fails on a stale panel or a name
  that is missing/extra, so this cannot rot silently.
- Library files are plain statements, wrapped into `function() … end` by the builder. That is why
  the panel can run them with no `loadstring`, which a plain LocalScript does not have.

```bash
python tools/build_menu.py          # regenerate the inline block
python tools/build_menu.py --check  # what a build should run first
```

Every entry also exposes its source, so the `edit` button drops it into the editor where it can be
changed and run as-is.

## Why it is shaped like this

- pure instances, no Drawing for the panel itself, so it survives resolution changes and does not
  fight the client's own GUI
- ESP uses `Drawing` (boxes, names) only because it has to sit over the 3D view
- motion is one curve (0.16 s quad out) for open, close and every hover — no snap anywhere
- toasts report what a script did, so a script that silently no-ops is visible instead of confusing
- `unload()` disconnects every connection, stops the tweens' targets and clears ESP
- features are deliberately small: nothing needs a game-specific offset, so it works anywhere and
  breaks loudly instead of silently. That is also the honest answer to "how many games": any game
  the executor can reach, not one title.
