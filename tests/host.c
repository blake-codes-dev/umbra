/*  Native harness. Compiles core.c against a real Luau VM so the bus, the bridges and the
 *  runner are exercised rather than merely built. Runs under wine:
 *      tools/test_native.sh
 *
 *  It is not a Roblox emulator. It fakes only what Roblox adds on top of Luau - `game`,
 *  `task`, `loadstring`, `getfenv`/`setfenv` - and then asserts end to end:
 *      runner loads -> command in -> print captured out -> done flag -> file bridge works.
 */
#define UMBRA_HOST 1

#include "lua.h"
#include "lualib.h"
#include "luacode.h"

#include "../src/native/core.c"

#include <stdio.h>

static lua_State *L;
static int g_heartbeat = 0;      /* registry ref of the RunService.Heartbeat handler */
static int g_failures;
static HANDLE g_busFile;

#define CHECK(condition, label)                                        \
    do {                                                               \
        if (condition) {                                               \
            printf("  ok   %s\n", label);                              \
        } else {                                                       \
            printf("  FAIL %s\n", label);                              \
            g_failures++;                                              \
        }                                                              \
    } while (0)

/* ---------------------------------------------------------------- fakes */
static int fake_spawn(lua_State *state)
{
    /* run the thread body immediately: the harness pumps one step at a time anyway */
    int results = lua_gettop(state) - 1;
    lua_pushvalue(state, 1);
    for (int i = 2; i <= results + 1; i++) lua_pushvalue(state, i);
    lua_call(state, results, 0);
    return 0;
}

static int fake_wait(lua_State *state)
{
    (void)state;
    return 0;
}

static int fake_cancel(lua_State *state)
{
    (void)state;
    return 0;
}

static int fake_loadstring(lua_State *state)
{
    size_t length = 0;
    const char *source = lua_tolstring(state, 1, &length);
    if (!source) luaL_error(state, "loadstring expects a string");
    size_t size = 0;
    char *bytecode = luau_compile(source, length, 0, &size);
    if (!bytecode || size == 0) return 1;
    if (luau_load(state, "=harness", bytecode, size, 0) != 0) return 1; /* nil, err */
    return 1;
}

static int fake_getfenv(lua_State *state)
{
    lua_getfenv(state, 1);
    return 1;
}

static int fake_setfenv(lua_State *state)
{
    lua_setfenv(state, 1);
    return 1;
}

static int signal_connect(lua_State *state)
{
    lua_pushvalue(state, 2);
    g_heartbeat = lua_ref(state, -1);
    return 0;
}

/* game:GetService("RunService").Heartbeat:Connect(fn) - just enough shape for the runner */
static int get_service(lua_State *state)
{
    lua_createtable(state, 0, 1);
    lua_pushcclosurek(state, signal_connect, 0, 0, 0);
    lua_setfield(state, -2, "Connect");
    lua_setfield(state, -2, "Heartbeat");
    (void)state;
    return 1;
}

static void install_fakes(void)
{
    lua_createtable(L, 0, 1);
    lua_pushcclosurek(L, get_service, 0, 0, 0);
    lua_setfield(L, -2, "GetService");
    lua_setglobal(L, "game");

    lua_createtable(L, 0, 3);
    lua_pushcclosurek(L, fake_spawn, 0, 0, 0);
    lua_setfield(L, -2, "spawn");
    lua_pushcclosurek(L, fake_wait, 0, 0, 0);
    lua_setfield(L, -2, "wait");
    lua_pushcclosurek(L, fake_cancel, 0, 0, 0);
    lua_setfield(L, -2, "cancel");
    lua_setglobal(L, "task");

    lua_pushcclosurek(L, fake_loadstring, 0, 0, 0);
    lua_setglobal(L, "loadstring");
    lua_pushcclosurek(L, fake_getfenv, 0, 0, 0);
    lua_setglobal(L, "getfenv");
    lua_pushcclosurek(L, fake_setfenv, 0, 0, 0);
    lua_setglobal(L, "setfenv");
}

/* ---------------------------------------------------------------- bus helpers */
static bus_header *header(void) { return (bus_header *)g.bus; }

static void queue_command(unsigned int op, unsigned int id, const char *payload)
{
    bus_header *h = header();
    char *slot = (char *)g.bus + CMD_BASE + (h->cmdHead % CMD_SLOTS) * CMD_SIZE;
    unsigned int length = (unsigned int)lstrlenA(payload);
    if (length > CMD_SIZE - 20) length = CMD_SIZE - 20;
    *(unsigned int *)(slot + 0) = op;
    *(unsigned int *)(slot + 4) = id;
    *(unsigned int *)(slot + 8) = length;
    for (unsigned int i = 0; i < length; i++) slot[12 + i] = payload[i];
    h->cmdHead = h->cmdHead + 1;
}

/* drain output slots into a flat transcript so assertions can look for text */
static unsigned int pump_output(char *transcript, size_t size, unsigned int *errors)
{
    bus_header *h = header();
    unsigned int seen = 0;
    transcript[0] = 0;
    while (h->outTail != h->outHead) {
        char *slot = (char *)g.bus + OUT_BASE + (h->outTail % OUT_SLOTS) * OUT_SIZE;
        unsigned int kind = *(unsigned int *)(slot + 0);
        unsigned int length = *(unsigned int *)(slot + 8);
        if (length > OUT_SIZE - 16) length = OUT_SIZE - 16;
        if (lstrlenA(transcript) + length + 8 < size) {
            lstrcatA(transcript, "[");
            char tag[2] = { (char)('0' + kind), 0 };
            lstrcatA(transcript, tag);
            lstrcatA(transcript, "]");
            for (unsigned int i = 0; i < length; i++) {
                size_t at = lstrlenA(transcript);
                transcript[at] = slot[12 + i];
                transcript[at + 1] = 0;
            }
            lstrcatA(transcript, "\n");
        }
        if (kind == 3 && errors) (*errors)++;
        h->outTail = h->outTail + 1;
        seen++;
    }
    return seen;
}

static void fire_heartbeat(void)
{
    if (!g_heartbeat) return;
    lua_getref(L, g_heartbeat);
    lua_call(L, 0, 0);
}

static int exec(const char *source, char *transcript, size_t size)
{
    unsigned int errors = 0;
    queue_command(OP_EXEC, 42, source);
    fire_heartbeat();
    pump_output(transcript, size, &errors);
    return errors;
}

/* ---------------------------------------------------------------- file helpers */
static char *slurp(const char *path)
{
    HANDLE file = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, 0, OPEN_EXISTING, 0, 0);
    DWORD size, read = 0;
    char *buffer;
    if (file == INVALID_HANDLE_VALUE) return 0;
    size = GetFileSize(file, 0);
    buffer = (char *)VirtualAlloc(0, size + 1, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (buffer && ReadFile(file, buffer, size, &read, 0)) buffer[read] = 0;
    CloseHandle(file);
    return buffer;
}

int main(void)
{
    char transcript[16384];
    char busPath[MAX_PATH];
    char *apiSource, *runnerSource;
    HANDLE section;
    unsigned int errors = 0;

    printf("umbra native harness\n");

    /* the bus, exactly as the core creates it */
    GetTempPathA(MAX_PATH, busPath);
    lstrcatA(busPath, "umbra-harness-bus.bin");
    g_busFile = CreateFileA(busPath, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                            0, CREATE_ALWAYS, 0, 0);
    section = CreateFileMappingA(g_busFile, 0, PAGE_READWRITE, 0, BUS_SIZE, 0);
    g.bus = MapViewOfFile(section, FILE_MAP_WRITE | FILE_MAP_READ, 0, 0, BUS_SIZE);
    header()->magic = BUS_MAGIC;
    header()->version = BUS_VERSION;

    L = luaL_newstate();
    luaL_openlibs(L);
    g.state = L;
    install_fakes();

    /* resolve the API the way the core does, but from symbols: this also proves the typedefs
       match the real ABI, which is the part a wrong guess would break */
    g.load = (luau_load_t)luau_load;
    g.compile = (luau_compile_t)luau_compile;
    g.pcall = (lua_pcall_t)lua_pcall;
    g.pushcclosure = (lua_pushcclosure_t)lua_pushcclosurek;
    g.createtable = (lua_createtable_t)lua_createtable;
    g.setfield = (lua_setfield_t)lua_setfield;
    g.rawseti = (lua_rawseti_t)lua_rawseti;
    g.pushls = (lua_pushlstring_t)lua_pushlstring;
    g.pushnumber = (lua_pushnumber_t)lua_pushnumber;
    g.pushboolean = (lua_pushboolean_t)lua_pushboolean;
    g.tolstring = (lua_tolstring_t)lua_tolstring;
    g.tonumber = (lua_tonumber_t)lua_tonumberx;
    g.gettop = (lua_gettop_t)lua_gettop;

    apiSource = slurp("src/luau/api.luau");
    runnerSource = slurp("src/luau/runner.luau");
    CHECK(apiSource && runnerSource, "read api.luau and runner.luau");
    if (!apiSource || !runnerSource) return 1;

    spawn_runner(L, apiSource, runnerSource);
    pump_output(transcript, sizeof(transcript), 0);
    printf("  runner said: %s", transcript);
    CHECK(find_text(transcript, "runner up") != 0, "runner installed");
    CHECK(g_heartbeat != 0, "runner connected to Heartbeat");

    /* one script, print captured */
    exec("print('hello from umbra')", transcript, sizeof(transcript));
    printf("  exec 1: %s", transcript);
    CHECK(find_text(transcript, "hello from umbra") != 0, "print captured through the bus");
    CHECK(find_text(transcript, "[5]") != 0, "done flag written");

    /* the file bridge, through api.luau */
    exec("writefile('t/one.txt', 'written by a script') "
         "print(readfile('t/one.txt')) "
         "print(tostring(isfile('t/one.txt')))",
         transcript, sizeof(transcript));
    printf("  exec 2: %s", transcript);
    CHECK(find_text(transcript, "written by a script") != 0, "writefile then readfile round trip");
    CHECK(find_text(transcript, "true") != 0, "isfile reports true");

    /* sandbox escape attempt must fail, not write outside the workspace */
    exec("local ok = pcall(writefile, '../escape.txt', 'x') print(tostring(ok))",
         transcript, sizeof(transcript));
    printf("  exec 3: %s", transcript);
    CHECK(find_text(transcript, "false") != 0, ".. refused by the sandbox");

    /* globals do not leak between scripts, genv does */
    exec("shared.marker = 'set' print('first')", transcript, sizeof(transcript));
    exec("print(tostring(shared.marker))", transcript, sizeof(transcript));
    printf("  exec 4: %s", transcript);
    CHECK(find_text(transcript, "set") != 0, "getgenv/shared survives across scripts");

    /* a script error is reported, not fatal */
    errors = exec("error('deliberate')", transcript, sizeof(transcript));
    printf("  exec 5: %s", transcript);
    CHECK(errors > 0, "script error reported on the error channel");

    printf("%s (%d failures)\n", g_failures ? "FAILED" : "PASSED", g_failures);
    return g_failures ? 1 : 0;
}
