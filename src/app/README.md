# Lyphen panel

WPF front end. Windows only, net9.0-windows.

```
dotnet run --project src/app/Lyphen      # or build and start the exe
```

`Services/IBackend.cs` is the whole seam: `AttachAsync`, `Detach`, `Execute`,
plus `StateChanged` and `Output` events. `MockBackend` implements it so the panel
runs with no client. The pipe backend replaces the mock and nothing in the window
changes.

`MockBackend.AttachAsync` needs a pid; the empty-box case fails on purpose so the
error path stays visible.

Not done yet: pipe transport, settings persistence, script library on disk,
hotkey for execute, drag-to-resize the editor split.
