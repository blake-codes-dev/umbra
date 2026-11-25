# Harness

`host.c` compiles `src/native/core.c` against a **real Luau VM** and runs the whole chain
under wine: bus, bridges, runner, script execution, output capture.

It is not a Roblox emulator. It fakes only what Roblox adds on top of Luau (`game`, `task`,
`loadstring`, `getfenv`, `setfenv`) and then asserts:

- the runner installs and hooks Heartbeat
- a script's `print` comes back through the bus, and `done` is flagged
- the file bridge round-trips, and `..` cannot escape the workspace
- `shared` survives between scripts while globals do not
- a script error lands on the error channel instead of killing the runner

## Running it

```bash
git clone --depth 1 https://github.com/luau-lang/luau /tmp/luau
cmake -B /tmp/luaub -DCMAKE_TOOLCHAIN_FILE=<mingw toolchain> -DCMAKE_BUILD_TYPE=Release \
      -DLUAU_BUILD_CLI=OFF -DLUAU_BUILD_TESTS=OFF /tmp/luau
make -C /tmp/luaub -j8 Luau.VM Luau.Compiler Luau.Ast Luau.Common Luau.Bytecode
LUAU_BUILD=/tmp/luaub LUAU_SRC=/tmp/luau tools/test_native.sh
```

Needs `gcc-mingw-w64-x86-64`, `g++-mingw-w64-x86-64` and wine. Luau's VM is C++, so the
harness compiles as C++ and `core.c` is included from it — the same source that ships is the
one under test.

## What it has already caught

Both would have shipped: `luau_load` takes bytecode, not source (so `luau_compile` had to be
added to the chain), and `pcall` reads the callee from `top - (nargs + 1)`, so anything pushed
before the callee gets called instead. Also that the runner's `loadstring` must be assigned
after the bridge merge, or the bridge's raw version wins and per-script environments are never
applied.

## Limits

Real `Instance`/`Signal` types, the DataModel and the client's own scheduler are absent, so
`getconnections`, `task.spawn` semantics under load and yield-across-C behaviour still need the
live client. The harness proves the plumbing, not the client.
