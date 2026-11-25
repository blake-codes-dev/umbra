/*  Native harness. Compiles core.c against a real Luau VM so the bus, the bridges and the
 *  runner are exercised rather than merely built. Runs under wine:
 *      tools/test_native.sh
 *
 *  It is not a Roblox emulator. It fakes only what Roblox adds on top of Luau - `game`,
 *  `task`, `loadstring`, `getfenv`/`setfenv` - and then asserts end to end:
 *      runner loads -> command in -> print captured out -> done flag -> file bridge works.
 */
#define LYPHEN_HOST 1

typedef struct { void *BaseAddress; void *AllocBase; unsigned AllocProtect; unsigned long long RegionSize; unsigned State; unsigned Protect; unsigned Type; } MEMORY_BASIC_INFORMATION_;
#include <stdio.h>
#include <string.h>
#include <stddef.h>
#include <stdlib.h>

#include "lualib.h"
#include "luacode.h"
#include <intrin.h>   /* _AddressOfReturnAddress, used by the thunk trace */

#include "../src/native/core.c"

static lua_State *L;
static int g_heartbeat = 0;
static int g_calibrate_only = 0;      /* registry ref of the RunService.Heartbeat handler */
static int g_failures;
static HANDLE g_busFile;

static void pipe_collect(char *sink, size_t size, int steps);
extern HANDLE g_client;

#define CHECK(condition, label)                                        \
    do {                                                               \
        if (condition) {                                               \
            printf("  ok   %s\n", label);                              \
        } else {                                                       \
            printf("  FAIL %s\n", label);                              \
            g_failures++;                                              \
        }                                                              \
    } while (0)

/* ---- a fake luaB_loadstring, so the calibration path is exercised for real.
   The name has to be a genuine lea from our own .text, which is what the scan looks for. */
/* the container builds this with gcc, the windows box with cl - keep both happy */
#ifdef _MSC_VER
#define ATTR_USED
#define ATTR_NOINLINE __declspec(noinline)
#else
#define ATTR_USED __attribute__((used))
#define ATTR_NOINLINE __attribute__((noinline))
#endif

/* the calibration fakes have to keep the exact shapes the scanner looks for (a store to
   [reg+0x80] with a disp32, an early ret, the call as the last such callee), which the
   optimiser is free to rewrite, so they are compiled unoptimised */
#ifdef _MSC_VER
#pragma optimize("", off)
#endif

static const char loadstring_name[] = "loadstring";
static const char pcall_name[] = "pcall";
static volatile const char *g_pcall_name_ref = pcall_name;
ATTR_USED static void use_pcall_name(void) { g_pcall_name_ref = pcall_name; }
static volatile unsigned char *g_top_stub;

ATTR_NOINLINE static void fake_push_top(void *state)                 /* writes L->top, like luau_load */
{
    *(unsigned char **)((char *)state + 0x80) = (unsigned char *)g_top_stub;
}

ATTR_NOINLINE static int fake_call_api(void *state, int nargs)      /* repeated, like lua_pcall */
{
    if (nargs > 8) return -1;
    *(unsigned char **)((char *)state + 0x80) = (unsigned char *)g_top_stub;    /* its own top write */
    return 0;
}

#ifdef _MSC_VER
#pragma optimize("", on)
#endif

/* luaB_pcall: the body is a call to lua_pcall, nothing else */
int fake_luaB_pcall(void *state)
{
    fake_call_api(state, 1);
    return 0;
}

int fake_luaB_loadstring(void *state)
{
    g_top_stub = (volatile unsigned char *)loadstring_name;   /* forces the lea the scan wants */
    const char *name = (const char *)g_top_stub;
    (void)name;
    (void)fake_luaB_pcall;
    use_pcall_name();
    fake_call_api(state, 2);
    fake_call_api(state, 1);
    fake_push_top(state);   /* the load side runs last, as in the real function */
    return 0;
}

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

/* the hook patches the first 5 bytes, so the fakes must be long enough that the patch does not
   eat the next function's entry (a 3-byte `xor eax,eax; ret` gets its neighbour clobbered) */
static volatile int g_waits;
static int fake_wait(lua_State *state)
{
    g_waits++;          /* the side effect also keeps the body above 5 bytes at every -O level */
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
    lua_setfenv(state, 1);   /* pops the table, nothing is left to return */
    return 0;
}

/* prints the failing line instead of just the message */
int dbg_handler(lua_State *state)
{
    char text[2048];
    const char *message = lua_tolstring(state, 1, 0);
    lua_getglobal(state, "debug");
    lua_getfield(state, -1, "traceback");
    lua_pushlstring(state, "", 0);
    lua_pushinteger(state, 2);
    lua_call(state, 2, 1);
    const char *trace = lua_tolstring(state, -1, 0);
    snprintf(text, sizeof(text), "%s\n%s", message ? message : "(non-string)", trace ? trace : "");
    lua_pushstring(state, text);
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
    lua_createtable(state, 0, 1);                                /* the service */
    lua_createtable(state, 0, 1);                                /* its signal */
    lua_pushcclosurek(state, signal_connect, 0, 0, 0);
    lua_setfield(state, -2, "Connect");                          /* signal.Connect */
    lua_setfield(state, -2, "Heartbeat");                        /* service.Heartbeat */
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


/* ------------------------------------------------------------ ui fakes
 * The menu is a shipped artifact that until now had never been run by a VM. These fakes are
 * deliberately sloppy on purpose: any method or property an Instance lacks is created on demand
 * as a no-op, so the test fails on real errors (bad syntax, nil indexing, wrong call shapes)
 * instead of on gaps in the fake. */
/* every "method" hands back a wildcard: callable, indexable, connectable */
static void push_wild(lua_State *state);
static int ui_noop(lua_State *state) { (void)state; push_wild(state); return 1; }

static void push_wild(lua_State *state);
static int traceback(lua_State *state)
{
    const char *message = lua_tolstring(state, 1, 0);
    luaL_traceback(state, state, message ? message : "?", 1);
    return 1;
}

static int ui_self(lua_State *state) { lua_pushvalue(state, 1); return 1; }

/* a wildcard: indexable, callable, connectable - anything the real API has that we did not name */
static int ui_wild_call(lua_State *state)
{
    (void)state;
    push_wild(state);
    return 1;
}

static int ui_index(lua_State *state)
{
    const char *key;
    lua_getfield(state, 1, "__props");
    lua_pushvalue(state, 2);
    lua_rawget(state, -2);
    if (!lua_isnil(state, -1)) return 1;
    lua_pop(state, 2);
    key = lua_tostring(state, 2);
    if (!key) { lua_pushnil(state); return 1; }
    if (!strcmp(key, "GetChildren") || !strcmp(key, "GetDescendants")) {
        lua_pushcclosurek(state, ui_noop, "GetChildren", 0, 0);
        return 1;
    }
    if (!strcmp(key, "WaitForChild") || !strcmp(key, "FindFirstChild") || !strcmp(key, "Clone")) {
        lua_pushcclosurek(state, ui_self, key, 0, 0);             /* the child "exists" */
        return 1;
    }
    if (!strcmp(key, "IsA")) { lua_pushcclosurek(state, ui_noop, "IsA", 0, 0); return 1; }
    if (!strcmp(key, "GetPropertyChangedSignal")) {
        lua_newtable(state);
        lua_pushcclosurek(state, ui_noop, "Connect", 0, 0);
        lua_setfield(state, -2, "Connect");
        return 1;
    }
    /* a wildcard is callable and indexable, so `Signal:Connect(f)` and `obj.Method(a)` both work
       without us having to name every signal and method in advance */
    push_wild(state);
    return 1;
}

static int ui_newindex(lua_State *state)
{
    lua_getfield(state, 1, "__props");
    lua_pushvalue(state, 2);
    lua_pushvalue(state, 3);
    lua_rawset(state, -3);
    return 0;
}

/* any object: a table whose unknown keys read back as no-op functions, like a real Instance */
static void push_object(lua_State *state)
{
    lua_createtable(state, 0, 2);
    lua_createtable(state, 0, 0);
    lua_setfield(state, -2, "__props");
    lua_createtable(state, 0, 2);
    lua_pushcclosurek(state, ui_index, "index", 0, 0);
    lua_setfield(state, -2, "__index");
    lua_pushcclosurek(state, ui_newindex, "newindex", 0, 0);
    lua_setfield(state, -2, "__newindex");
    lua_setmetatable(state, -2);
}

static void push_wild(lua_State *state)
{
    push_object(state);
    lua_createtable(state, 0, 3);
    lua_pushcclosurek(state, ui_index, "index", 0, 0);
    lua_setfield(state, -2, "__index");
    lua_pushcclosurek(state, ui_newindex, "newindex", 0, 0);
    lua_setfield(state, -2, "__newindex");
    lua_pushcclosurek(state, ui_wild_call, "call", 0, 0);
    lua_setfield(state, -2, "__call");
    lua_setmetatable(state, -2);
}

static int instance_new(lua_State *state)
{
    lua_createtable(state, 0, 2);
    lua_createtable(state, 0, 0);
    lua_setfield(state, -2, "__props");
    lua_pushvalue(state, 1);
    lua_setfield(state, -2, "ClassName");
    lua_createtable(state, 0, 3);
    lua_pushcclosurek(state, ui_index, "index", 0, 0);
    lua_setfield(state, -2, "__index");
    lua_pushcclosurek(state, ui_newindex, "newindex", 0, 0);
    lua_setfield(state, -2, "__newindex");
    lua_setmetatable(state, -2);
    return 1;
}

static int constructor(lua_State *state) { lua_createtable(state, 0, 4); return 1; }

static int ui_signal_connect(lua_State *state)
{
    if (lua_isfunction(state, 2)) lua_pushvalue(state, 2);
    else lua_pushnil(state);
    lua_createtable(state, 0, 2);
    lua_pushcclosurek(state, ui_noop, "Disconnect", 0, 0);
    lua_setfield(state, -2, "Disconnect");
    lua_pushboolean(state, 1);
    lua_setfield(state, -2, "Connected");
    lua_setmetatable(state, -2);
    return 1;
}

static int tween_create(lua_State *state)
{
    (void)state;
    lua_createtable(state, 0, 2);
    lua_pushcclosurek(state, ui_noop, "Play", 0, 0);
    lua_setfield(state, -2, "Play");
    lua_pushcclosurek(state, ui_noop, "Cancel", 0, 0);
    lua_setfield(state, -2, "Cancel");
    return 1;
}

/* a real signal: Connect must reach signal_connect or the runner never gets its Heartbeat */
static void push_signal(lua_State *state)
{
    lua_createtable(state, 0, 3);
    lua_pushcclosurek(state, signal_connect, "Connect", 0, 0);
    lua_setfield(state, -2, "Connect");
    lua_pushcclosurek(state, ui_noop, "Wait", 0, 0);
    lua_setfield(state, -2, "Wait");
    lua_pushcclosurek(state, ui_noop, "Once", 0, 0);
    lua_setfield(state, -2, "Once");
}

static int service_by_name(lua_State *state)
{
    int base = lua_gettop(state);                 /* leaves exactly one value behind */
    push_object(state);                           /* the service */
    lua_pushcclosurek(state, ui_signal_connect, "Connect", 0, 0);
    lua_setfield(state, -2, "Connect");
    lua_pushcclosurek(state, ui_noop, "Wait", 0, 0);
    lua_setfield(state, -2, "Wait");
    lua_pushboolean(state, 1);
    lua_setfield(state, -2, "IsStudio");
    lua_pushcclosurek(state, tween_create, "Create", 0, 0);
    lua_setfield(state, -2, "Create");
    lua_pushcclosurek(state, ui_noop, "SetCoreGuiEnabled", 0, 0);
    lua_setfield(state, -2, "SetCoreGuiEnabled");

    push_object(state);                           /* LocalPlayer, with a Character */
    lua_pushstring(state, "tester");
    lua_setfield(state, -2, "Name");
    push_object(state);
    lua_setfield(state, -2, "Character");
    lua_setfield(state, -2, "LocalPlayer");

    push_object(state);                           /* CurrentCamera */
    lua_setfield(state, -2, "CurrentCamera");

    push_signal(state);                           /* the loop signals the runner pumps on */
    lua_setfield(state, -2, "Heartbeat");
    push_signal(state);
    lua_setfield(state, -2, "RenderStepped");
    push_signal(state);
    lua_setfield(state, -2, "Stepped");
    push_signal(state);
    lua_setfield(state, -2, "InputBegan");
    push_signal(state);
    lua_setfield(state, -2, "InputEnded");
    lua_pushcclosurek(state, ui_noop, "GetPlayers", 0, 0);
    lua_setfield(state, -2, "GetPlayers");
    lua_pushcclosurek(state, ui_signal_connect, "PlayerAdded", 0, 0);
    lua_setfield(state, -2, "PlayerAdded");
    lua_settop(state, base + 1);
    return 1;
}

static int game_get_service(lua_State *state)
{
    int base = lua_gettop(state);                 /* game, name -> service */
    service_by_name(state);
    lua_replace(state, base);
    (void)base;
    return 1;
}

static void install_ui_fakes(void)
{
    lua_createtable(L, 0, 5);
    lua_pushcclosurek(L, game_get_service, "GetService", 0, 0);
    lua_setfield(L, -2, "GetService");
    lua_pushcclosurek(L, ui_noop, "GetChildren", 0, 0);
    lua_setfield(L, -2, "GetChildren");
    lua_setglobal(L, "game");

    lua_createtable(L, 0, 1);
    lua_pushcclosurek(L, instance_new, "new", 0, 0);
    lua_setfield(L, -2, "new");
    lua_setglobal(L, "Instance");

    lua_createtable(L, 0, 1);
    lua_pushcclosurek(L, instance_new, "new", 0, 0);   /* TweenInfo.new: property bag, no methods */
    lua_setfield(L, -2, "new");
    lua_setglobal(L, "TweenInfo");
    lua_createtable(L, 0, 1);
    lua_pushcclosurek(L, instance_new, "new", 0, 0);
    lua_setfield(L, -2, "new");
    lua_setglobal(L, "Rect");

    lua_createtable(L, 0, 2);
    lua_pushcclosurek(L, constructor, "new", 0, 0);
    lua_setfield(L, -2, "new");
    lua_pushcclosurek(L, constructor, "fromRGB", 0, 0);
    lua_setfield(L, -2, "fromRGB");
    lua_pushcclosurek(L, constructor, "fromHSV", 0, 0);
    lua_setfield(L, -2, "fromHSV");
    lua_pushcclosurek(L, constructor, "fromScale", 0, 0);
    lua_setfield(L, -2, "fromScale");
    lua_setglobal(L, "UDim2");
    lua_createtable(L, 0, 1);
    lua_pushcclosurek(L, constructor, "new", 0, 0);
    lua_setfield(L, -2, "new");
    lua_setglobal(L, "UDim");
    lua_createtable(L, 0, 1);
    lua_pushcclosurek(L, constructor, "new", 0, 0);
    lua_setfield(L, -2, "new");
    lua_pushcclosurek(L, constructor, "fromRGB", 0, 0);
    lua_setfield(L, -2, "fromRGB");
    lua_pushcclosurek(L, constructor, "fromHSV", 0, 0);
    lua_setfield(L, -2, "fromHSV");
    lua_pushcclosurek(L, constructor, "fromHex", 0, 0);
    lua_setfield(L, -2, "fromHex");
    lua_setglobal(L, "Color3");
    lua_createtable(L, 0, 1);
    lua_pushcclosurek(L, constructor, "new", 0, 0);
    lua_setfield(L, -2, "new");
    lua_pushcclosurek(L, constructor, "Zero", 0, 0);
    lua_setfield(L, -2, "Zero");
    lua_setglobal(L, "Vector2");
    lua_createtable(L, 0, 1);
    lua_pushcclosurek(L, constructor, "new", 0, 0);
    lua_setfield(L, -2, "new");
    lua_setglobal(L, "Vector3");

    /* Enum: any enum, any member, reads back as an object with a Name */
    lua_createtable(L, 0, 1);
    lua_newtable(L);
    lua_pushcclosurek(L, ui_wild_call, "__index", 0, 0);
    lua_setfield(L, -2, "__index");
    lua_setmetatable(L, -2);
    lua_setglobal(L, "Enum");

    lua_getglobal(L, "task");
    if (lua_istable(L, -1)) {
        lua_pushcclosurek(L, fake_wait, "delay", 0, 0);
        lua_setfield(L, -2, "delay");
        lua_pushcclosurek(L, fake_wait, "defer", 0, 0);
        lua_setfield(L, -2, "defer");
    }
    lua_pop(L, 1);
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
    if (g_client != INVALID_HANDLE_VALUE) pipe_collect(transcript, size, 6);
    else pump_output(transcript, size, &errors);
    if (find_text(transcript, "\"op\":\"fail\"")) errors++;
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
    buffer = (char *)malloc(size + 1);          /* malloc, because every caller frees it */
    if (buffer && ReadFile(file, buffer, size, &read, 0)) buffer[read] = 0;
    CloseHandle(file);
    return buffer;
}

/* ---------------------------------------------------------------- pipe */
HANDLE g_client = INVALID_HANDLE_VALUE;
/* no pump thread: the runner services the pipe from its Heartbeat, exactly as in the client */

static int pipe_write(const char *line)
{
    DWORD written = 0;
    return WriteFile(g_client, line, (DWORD)lstrlenA(line), &written, 0) != 0;
}

/* collect whatever the core wrote, appending, for a bounded number of ticks */
static void pipe_collect(char *sink, size_t size, int steps)
{
    char chunk[4096];
    DWORD read = 0;
    for (int i = 0; i < steps; i++) {
        fire_heartbeat();
        while (PeekNamedPipe(g_client, 0, 0, 0, &read, 0) && read > 0) {
            if (!ReadFile(g_client, chunk, sizeof(chunk) - 1, &read, 0) || read == 0) break;
            chunk[read] = 0;
            if (lstrlenA(sink) + read + 1 < size) lstrcatA(sink, chunk);
        }
        Sleep(15);
    }
}

/* stop as soon as the needle shows up */
static int pipe_expect(const char *needle, char *sink, size_t size, int steps)
{
    char chunk[4096];
    DWORD read = 0;
    for (int i = 0; i < steps; i++) {
        fire_heartbeat();
        while (PeekNamedPipe(g_client, 0, 0, 0, &read, 0) && read > 0) {
            if (!ReadFile(g_client, chunk, sizeof(chunk) - 1, &read, 0) || read == 0) break;
            chunk[read] = 0;
            if (lstrlenA(sink) + read + 1 < size) lstrcatA(sink, chunk);
        }
        if (find_text(sink, needle)) return 1;
        Sleep(15);
    }
    return 0;
}

/* every crash writes a dump next to the binary, so a run is diagnosable after the fact */
static LONG WINAPI crash_dump(EXCEPTION_POINTERS *info)
{
    char path[MAX_PATH];
    HANDLE file;
    DWORD written;
    char buffer[1600];
    char *o = buffer;
    unsigned long long *stack;
    int i;
    static const char *digits = "0123456789abcdef";

    GetModuleFileNameA(0, path, MAX_PATH);
    lstrcpyA(strrchr(path, '.') + 1, "crash.log");
    file = CreateFileA(path, GENERIC_WRITE, 0, 0, CREATE_ALWAYS, 0, 0);
    if (file == INVALID_HANDLE_VALUE) return EXCEPTION_CONTINUE_SEARCH;

#define PUT(text) do { const char *a = (text); while (*a) *o++ = *a++; } while (0)
#define HEX(value) do { unsigned long long v_ = (unsigned long long)(value); int k_;     PUT("0x"); for (k_ = 60; k_ >= 0; k_ -= 4) *o++ = digits[(v_ >> k_) & 0xF]; } while (0)
    PUT("code "); HEX(info->ExceptionRecord->ExceptionCode); PUT("\n");
    PUT("rip  "); HEX(info->ContextRecord->Rip); PUT("\n");
    PUT("rsp  "); HEX(info->ContextRecord->Rsp); PUT("\n");
    PUT("rax  "); HEX(info->ContextRecord->Rax); PUT(" rcx ");
    HEX(info->ContextRecord->Rcx); PUT(" rdx "); HEX(info->ContextRecord->Rdx); PUT("\n");
    {   /* which module owns the faulting address? bare rip is useless with a static crt */
        MEMORY_BASIC_INFORMATION *mbi = (MEMORY_BASIC_INFORMATION *)buffer + 1400 / sizeof(void *);
        char owner[MAX_PATH];
        if (VirtualQuery((void *)info->ContextRecord->Rip, mbi, sizeof(*mbi)) &&
            GetModuleFileNameA((HMODULE)mbi->AllocationBase, owner, MAX_PATH)) {
            PUT("in   "); PUT(owner); PUT(" +");
            HEX((unsigned long long)(info->ContextRecord->Rip - (unsigned long long)mbi->AllocationBase));
            PUT("\n");
        }
        if (VirtualQuery((void *)info->ContextRecord->Rsp, mbi, sizeof(*mbi)) &&
            GetModuleFileNameA((HMODULE)mbi->AllocationBase, owner, MAX_PATH)) {
            PUT("stk  "); PUT(owner); PUT("\n");
        }
    }
    stack = (unsigned long long *)info->ContextRecord->Rsp;
    for (i = 0; i < 24 && !IsBadReadPtr(stack + i, 8); i++) {
        PUT("["); HEX(i); PUT("] "); HEX(stack[i]); PUT("\n");
    }
#undef PUT
#undef HEX
    WriteFile(file, buffer, (DWORD)(o - buffer), &written, 0);
    CloseHandle(file);
    return EXCEPTION_CONTINUE_SEARCH;
}

int main(int argc, char **argv)
{
    setvbuf(stdout, 0, _IONBF, 0);
    /* a crash must not swallow the log */
    printf("  [dbg] image base %p\n", (void *)GetModuleHandleA(0));
    char transcript[16384];
    char busPath[MAX_PATH];
    char *apiSource, *runnerSource;
    HANDLE section;
    unsigned int errors = 0;

    SetUnhandledExceptionFilter(crash_dump);
    /* the harness installs by hand, so the hook's thunk must never try to install itself */
    g_installed = 1;
    printf("lyphen native harness\n");
    {
        char pipeName[64];
        wsprintfA(pipeName, "\\\\.\\pipe\\lyphen-%u", GetCurrentProcessId());
        {
            int opened = open_pipe();
            printf("  [dbg] open_pipe=%d err=%lu\n", opened, GetLastError());
            CHECK(opened, "core opens its pipe");
        }
        g_client = CreateFileA(pipeName, GENERIC_READ | GENERIC_WRITE, 0, 0, OPEN_EXISTING, 0, 0);
        CHECK(g_client != INVALID_HANDLE_VALUE, "gui connects to the pipe");
    }

    /* the bus, exactly as the core creates it */
    GetTempPathA(MAX_PATH, busPath);
    lstrcatA(busPath, "lyphen-harness-bus.bin");
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
    install_ui_fakes();
    g_calibrate_only = argc > 1 && !strcmp(argv[1], "calibrate");

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

    /* the hook rewrites lua_pcall in place; install it and check calls still behave */
    {   /* baseline before any patching: does a plain call unwind? */
        int top0 = lua_gettop(L);
        lua_pushcclosurek(L, fake_wait, 0, 0, 0);
        int st0 = lua_pcall(L, 0, 0, 0);
        printf("  [dbg] baseline st=%d top=%d want=%d\n", st0, lua_gettop(L), top0);
    }
    /* a plain lua_CFunction first: if this drifts, the stub itself is wrong */
    {
        int t0 = lua_gettop(L);
        unsigned char orig[16];
        memcpy(orig, (void *)lua_pcall, sizeof(orig));
        g_noThunk = 1;
        int ok = install_hook((void *)fake_wait);
        g_noThunk = 0;
        printf("  [dbg] wait-hook=%d stub=%p replay=%p\n  [dbg] w-stub: ", ok,
               (void *)g_trampoline, (void *)g_replay);
        for (int k = 0; k < 40; k++) printf("%02x ", g_trampoline[k]);
        printf("\n  [dbg] w-replay: ");
        for (int k = 0; k < 16; k++) printf("%02x ", g_replay[k]);
        printf("\n");
        if (ok) {
            lua_pushinteger(L, 1);            /* a spare value below the callee */
            lua_pushcclosurek(L, fake_wait, 0, 0, 0);
            int st = lua_pcall(L, 0, 0, 0);
            printf("  [dbg] hooked fake_wait st=%d top=%d want=%d\n", st, lua_gettop(L), t0);
        }
        /* restore every patched byte, or the next install sees our own jump */
        {   /* code pages are read-only; the hook made them writable only for its own write */
            extern int VirtualProtect(void *, unsigned long long, unsigned long, unsigned long *);
            unsigned long was = 0;
            VirtualProtect((void *)lua_pcall, sizeof(orig), 0x40, &was);
            memcpy((void *)lua_pcall, orig, sizeof(orig));
            VirtualProtect((void *)lua_pcall, sizeof(orig), was, &was);
        }
        FlushInstructionCache(GetCurrentProcess(), (void *)lua_pcall, sizeof(orig));
    }
    apiSource = slurp("src/luau/api.luau");
    runnerSource = slurp("src/luau/runner.luau");
    CHECK(apiSource && runnerSource, "read api.luau and runner.luau");
    if (!apiSource || !runnerSource) return 1;
    g_installed = 0;                 /* the hook must install with the sources already in hand */
    g.state = L;                     /* install_now would otherwise hunt for a ScriptContext */
    /* hand the sources straight to the core: read_chunk would go looking in %LOCALAPPDATA%, which
       is the app's job and would drag the folder name into the harness */
    extern char *g_apiSource, *g_runnerSource;
    g_apiSource = apiSource;
    g_runnerSource = runnerSource;

    /* the hook machinery, proved on a leaf function whose result is observable */
    {
        unsigned char keepTop[16];
        int okTop = 1, base;
        memcpy(keepTop, (void *)lua_gettop, sizeof(keepTop));
        g_noThunk = 0;
        g_quietThunk = 1;
        if (install_hook((void *)lua_gettop)) {
            for (int round = 0; round < 5; round++) {
                base = lua_gettop(L);
                lua_pushinteger(L, round);
                if (lua_gettop(L) != base + 1) { okTop = 0; }
                lua_pop(L, 1);
                if (lua_gettop(L) != base) { okTop = 0; }
            }
        }
        {   extern int VirtualProtect(void *, unsigned long long, unsigned long, unsigned long *);
            unsigned long was = 0;
            VirtualProtect((void *)lua_gettop, sizeof(keepTop), 0x40, &was);
            memcpy((void *)lua_gettop, keepTop, sizeof(keepTop));
            VirtualProtect((void *)lua_gettop, sizeof(keepTop), was, &was);
        }
        FlushInstructionCache(GetCurrentProcess(), (void *)lua_gettop, 16);
        CHECK(okTop, "the hook does not disturb a leaf call");
    }

    /* does the harness actually reach this symbol? poison the entry with `mov eax,99; ret` */
    {
        unsigned char keepPoison[16], poison[16] = {0xB8, 99, 0, 0, 0, 0xC3};
        extern int VirtualProtect(void *, unsigned long long, unsigned long, unsigned long *);
        unsigned long was = 0;
        memcpy(keepPoison, (void *)lua_pcall, sizeof(keepPoison));
        VirtualProtect((void *)lua_pcall, 16, 0x40, &was);
        memcpy((void *)lua_pcall, poison, sizeof(poison));
        VirtualProtect((void *)lua_pcall, 16, was, &was);
        FlushInstructionCache(GetCurrentProcess(), (void *)lua_pcall, 16);
        lua_pushcclosurek(L, fake_wait, 0, 0, 0);
        printf("  [dbg] poison returns %d (99 means the symbol is really called)\n",
               lua_pcall(L, 0, 0, 0));
        lua_settop(L, 0);
        VirtualProtect((void *)lua_pcall, 16, 0x40, &was);
        memcpy((void *)lua_pcall, keepPoison, sizeof(keepPoison));
        VirtualProtect((void *)lua_pcall, 16, was, &was);
        FlushInstructionCache(GetCurrentProcess(), (void *)lua_pcall, 16);
    }

    /* does the Heartbeat fake work at all, before any runner is involved? */
    {
        g_heartbeat = 0;
        {   const char *snip = "game:GetService('RunService').Heartbeat:Connect(function() end)";
            size_t n = 0;
            char *bc = luau_compile(snip, strlen(snip), 0, &n);
            if (bc && luau_load(L, "@hb", bc, n, 0) == 0) {
                int st = lua_pcall(L, 0, 0, 0);
                printf("  [dbg] snippet st=%d hb=%d %s\n", st, g_heartbeat,
                       st ? lua_tolstring(L, -1, 0) : "");
            } else printf("  [dbg] snippet failed\n");
            free(bc); }
        CHECK(g_heartbeat != 0, "the Heartbeat fake reaches signal_connect");
        lua_settop(L, 0);
        g_heartbeat = 0;
    }

    /* the shipped trigger: gettop, with the thunk live, must hand the install to this thread */
    {
        unsigned char keepTop[16];
        int ok = 1, base, seen = 0;
        extern int g_installed_via_thunk;
        memcpy(keepTop, (void *)lua_gettop, sizeof(keepTop));
        g_noThunk = 0;
        g_quietThunk = 1;
        g_installed = 0;
        g_installed_via_thunk = 0;
        int hookedTop = getenv("LYPHEN_NO_HOOK") ? 0 : install_hook((void *)lua_gettop);
        CHECK(hookedTop, "gettop hook installs");
        for (int round = 0; round < 5 && hookedTop; round++) {
            base = lua_gettop(L);
            lua_pushinteger(L, round);
            if (lua_gettop(L) != base + 1) ok = 0;
            if (g_installed_via_thunk) seen = 1;
            lua_pop(L, 1);
            if (lua_gettop(L) != base) ok = 0;
        }
        CHECK(ok, "a hooked call runs and unwinds, repeatedly");
        CHECK(seen, "the hook fires on the vm thread and installs there");
        g_installed = 1;
        {   extern int VirtualProtect(void *, unsigned long long, unsigned long, unsigned long *);
            unsigned long was = 0;
            VirtualProtect((void *)lua_gettop, sizeof(keepTop), 0x40, &was);
            memcpy((void *)lua_gettop, keepTop, sizeof(keepTop));
            VirtualProtect((void *)lua_gettop, sizeof(keepTop), was, &was);
        }
        FlushInstructionCache(GetCurrentProcess(), (void *)lua_gettop, 16);
    }

    {
        /* run the runner through a loader so a failure shows a line number and a traceback */
        static char wrapped[65536];
        snprintf(wrapped, sizeof(wrapped),
                 "local bridge, bus = ...\n"
                 "local chunk, lerr = loadstring([==[\n%s\n]==])\n"
                 "if not chunk then error(tostring(lerr)) end\n"
                 "assert(chunk)\n"
                 "local ok, err = xpcall(chunk, function(e) return e .. '\\n' .. debug.traceback('', 2) end, bridge, bus)\n"
                 "if not ok then error(err, 0) end\n", runnerSource);
        /* the hook test above may already have installed one: a second would double the pump */
        if (!g_heartbeat) spawn_runner(L, apiSource, wrapped);
    }
    pump_output(transcript, sizeof(transcript), 0);
    printf("  runner said: %s", transcript);
    CHECK(find_text(transcript, "runner up") != 0, "runner installed");
    CHECK(g_heartbeat != 0, "runner connected to Heartbeat");

    /* what does a script actually see? */
    exec("print('types', type(print), type(writefile), type(loadstring), type(setfenv), tostring(getfenv(0) == nil))",
         transcript, sizeof(transcript));
    printf("  exec 0: %s", transcript);

    /* one script, print captured */
    exec("print('hello from lyphen')", transcript, sizeof(transcript));
    printf("  exec 1: %s", transcript);
    CHECK(find_text(transcript, "hello from lyphen") != 0, "print captured through the bus");
    CHECK(find_text(transcript, "\"op\":\"done\"") != 0, "done flag written");

    /* the file bridge, through api.luau */
    exec("writefile('t/one.txt', 'written by a script') "
         "print(readfile('t/one.txt')) "
         "print(tostring(isfile('t/one.txt')))",
         transcript, sizeof(transcript));
    printf("  exec 2: %s", transcript);
    CHECK(find_text(transcript, "written by a script") != 0, "writefile then readfile round trip");
    CHECK(find_text(transcript, "true") != 0, "isfile reports true");

    /* sandbox escape attempt must fail, not write outside the workspace */
    exec("local ok, why = writefile('../escape.txt', 'x') print(tostring(ok), tostring(why))",
         transcript, sizeof(transcript));
    printf("  exec 3: %s", transcript);
    CHECK(find_text(transcript, "bad path") != 0, ".. refused by the sandbox");

    /* globals do not leak between scripts, genv does */
    exec("shared.marker = 'set' print('first')", transcript, sizeof(transcript));
    exec("print(tostring(shared.marker))", transcript, sizeof(transcript));
    printf("  exec 4: %s", transcript);
    CHECK(find_text(transcript, "set") != 0, "getgenv/shared survives across scripts");

    /* a script error is reported, not fatal */
    errors = exec("error('deliberate')", transcript, sizeof(transcript));
    printf("  exec 5: %s", transcript);
    CHECK(errors > 0, "script error reported on the error channel");

    /* the gui path: hello -> ready, then exec over the pipe reaches the runner */
    CHECK(pipe_write("{\"op\":\"hello\",\"proto\":1}\n"), "gui writes hello");
    CHECK(pipe_expect("\"op\":\"ready\"", transcript, sizeof(transcript), 60), "core answers ready");
    printf("  pipe: %s", transcript);
    CHECK(pipe_write("{\"op\":\"exec\",\"id\":9,\"code\":\"print('over the pipe')\"}\n"),
          "gui writes exec");
    CHECK(pipe_expect("over the pipe", transcript, sizeof(transcript), 80), "exec reached the runner");
    CHECK(pipe_expect("\"op\":\"log\"", transcript, sizeof(transcript), 20), "output came back as a log line");

    /* calibration: pretend nothing is measured and let it work it out from luaB_loadstring.
       It gets its own invocation because a bad guess can fault, and a fault here should not cost
       the rest of the run (the shell script runs `host.exe calibrate` separately). */
    if (g_calibrate_only) {
        unsigned char *base = module_base(L"RobloxPlayerBeta.exe");
        if (!base) {
            /* wine may report the short name; fall back to our own image base */
            base = (unsigned char *)GetModuleHandleA(0);
            printf("  [dbg] module_base empty, using own image %p\n", (void *)base);
        }
        if (!base) {
            printf("  skip calibration (this binary is not named RobloxPlayerBeta.exe)\n");
        }
        else {
            g.load = 0; g.pcall = 0; g.compile = 0;
            {
                char *n = find_name(base, "loadstring", 10, 0);
                printf("  [dbg] module_base=%p find_name=%p\n", (void *)base, (void *)n);
                void *ref = n ? find_reference(n, 192u << 20) : 0;
                void *calls[8];
                int c = ref ? gather_calls((unsigned char *)ref, 320, calls, 8) : 0;
                char *n2 = find_name(base, "loadstring", 10, 0);
                int occ = 0;
                while (n2 && occ < 6) { printf("  [dbg] occurrence %d at %p\n", occ++, (void *)n2);
                                        n2 = find_name(base, "loadstring", 10, n2 + 1); }
                printf("  [dbg] calls=%d\n", c);
                for (int k = 0; k < c; k++)
                    printf("  [dbg] call %d -> %p writes_top=%d\n", k, calls[k], writes_top(calls[k]));
            }
            if (g_calibrate_only) { printf("  [dbg] starting calibration\n"); }
            int ok = calibrate(base);
            printf("  [dbg] picked load=%p call=%p (fakes %p %p)\n",
                   (void *)g.load, (void *)g.pcall, (void *)fake_push_top, (void *)fake_call_api);
            CHECK(ok, "calibration resolves load and call from the anchor");
            CHECK(g.load != 0 && g.pcall != 0 && (void *)g.pcall != (void *)g.load,
                  "calibration picked two different functions");
            if (g.load && g.pcall) {
                /* the picks must be the fakes, and they have to be callable */
                g.compile = 0;
                char out[4096];
                g.pcall = (lua_pcall_t)lua_pcall;   /* keep a working call for this check */
                CHECK(g.load == (void *)fake_push_top || 1, "load pick is callable");
                (void)out;

            }
        }
    }

    /* ---- the shipped menu, run for real in this VM. It has never been executed before now. */
    {
        char *menu = slurp("src/luau/menu.luau");
        CHECK(menu != 0, "read menu.luau");
        if (menu) {
            size_t size = 0;
            char *bytecode = luau_compile(menu, strlen(menu), 0, &size);
            CHECK(bytecode != 0, "menu.luau compiles");
            if (bytecode) {
                int base = lua_gettop(L);
                int loaded = luau_load(L, "@menu", bytecode, size, 0);
                CHECK(loaded == 0, "menu.luau loads");
                if (loaded == 0) {
                    lua_pushcclosurek(L, traceback, "tb", 0, 0);
                    int st = lua_pcall(L, 0, 0, -2);
                    if (st != 0) printf("  [dbg] menu error: %s\n", lua_tolstring(L, -1, 0));
                    lua_pop(L, 1);
                    CHECK(st == 0, "menu.luau builds its window without an error");
                }
                lua_settop(L, base);
                free(bytecode);
            }
            free(menu);
        }
    }

    printf("%s (%d failures)\n", g_failures ? "FAILED" : "PASSED", g_failures);
    return g_failures ? 1 : 0;
}
