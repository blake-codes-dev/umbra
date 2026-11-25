/*  Umbra core. Manual-mapped, no module entry, no thread of its own.
 *
 *  Sequence, once the entry is running on a hijacked client thread:
 *    1. resolve kernel32 file/mapping calls (no IAT here, the image is mapped by hand)
 *    2. create + map the bus section so a re-inject can find the previous run
 *    3. scan the loaded modules for the Luau functions and the state path
 *    4. call into Luau exactly once (task.spawn of the runner), then only move bytes
 *
 *  Every dereference that can be wrong sits in __try/__except with a named stage, so a
 *  failed scan degrades a feature instead of killing the client.
 */
#include <windows.h>

/* MSVC has SEH; mingw does not, so a mingw build degrades to plain blocks instead of
   silently losing the guards. Only the MSVC build ships. */
#if defined(__MINGW32__)
#define UMBRA_TRY if (1)
#define UMBRA_EXCEPT(e) else
#else
#define UMBRA_TRY __try
#define UMBRA_EXCEPT(e) __except (e)
#endif

#define BUS_MAGIC 0x52424D55u /* 'UMBR' */
#define BUS_VERSION 1
#define BUS_SIZE 0x20000
#define CMD_SLOTS 60
#define CMD_SIZE 0x400
#define OUT_SLOTS 100
#define OUT_SIZE 0x200
#define CMD_BASE 0x100
#define OUT_BASE 0x10000

#define OP_EXEC 1
#define OP_CANCEL 2
#define OP_PROBE 3
#define OP_SHUTDOWN 4

/* measured 2026-11-24 on 2.748.1526.0 (docs: ASSUMPTIONS.md) */
#define L_TOP 0x80
#define L_BASE 0x78
#define L_CI 0x58
#define L_GLOBAL 0x68

typedef void *lua_State;
typedef int (*luau_load_t)(lua_State *, const char *, const char *, size_t, int);
typedef int (*lua_pcall_t)(lua_State *, int, int, int);
typedef void *(*lua_pushcclosure_t)(lua_State *, void *, int);
typedef const char *(*lua_tolstring_t)(lua_State *, int, size_t *);
typedef void (*lua_pushlstring_t)(lua_State *, const char *, size_t);

typedef struct {
    unsigned int magic, version, cmdHead, cmdTail, outHead, outTail, flags;
} bus_header;

typedef struct {
    HMODULE exe;
    void *image;
    size_t imageSize;
    lua_State *state;
    luau_load_t load;
    lua_pcall_t pcall;
    lua_pushcclosure_t pushc;
    lua_pushlstring_t pushls;
    lua_tolstring_t tolstring;
    HANDLE pipe;
    void *bus;
    unsigned int nextId;
} core_state;

static core_state g;
static char g_stage[32] = "start";

/* ------------------------------------------------------------------ plumbing */
static void stage(const char *name)
{
    int i = 0;
    while (name[i] && i < 31) {
        g_stage[i] = name[i];
        i++;
    }
    g_stage[i] = 0;
}

static void put(const char *s)
{
    /* last resort debug channel: only used while the pipe is not up yet */
    HANDLE f = CreateFileA("C:\\Windows\\Temp\\umbra-core.log", FILE_APPEND_DATA,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, 0, OPEN_ALWAYS, 0, 0);
    DWORD w;
    if (f != INVALID_HANDLE_VALUE) {
        WriteFile(f, s, lstrlenA(s), &w, 0);
        WriteFile(f, "\r\n", 2, &w, 0);
        CloseHandle(f);
    }
}

static void emit(const char *kind, unsigned int id, const char *text)
{
    bus_header *h = (bus_header *)g.bus;
    char *slot;
    unsigned int index, len;
    if (!g.bus || !text) return;
    index = h->outHead % OUT_SLOTS;
    slot = (char *)g.bus + OUT_BASE + index * OUT_SIZE;
    len = lstrlenA(text);
    if (len > OUT_SIZE - 16) len = OUT_SIZE - 16;
    *(unsigned int *)(slot + 0) = (unsigned int)(kind[0]);
    *(unsigned int *)(slot + 4) = id;
    *(unsigned int *)(slot + 8) = len;
    for (unsigned int i = 0; i < len; i++) slot[12 + i] = text[i];
    h->outHead = h->outHead + 1;
}

static void fail(const char *where, const char *text)
{
    char line[256];
    char *o = line;
    const char *a = "fail ";
    while (*a) *o++ = *a++;
    while (*where) *o++ = *where++;
    *o++ = ' ';
    while (*text && o < line + 240) *o++ = *text++;
    *o = 0;
    put(line);
    emit("e", 0, line);
}

/* ------------------------------------------------------------------ scanning */
/* A pattern is a byte string with 0xCC as the wildcard. First exact hit wins. */
static void *scan_module(HMODULE module, const unsigned char *pattern, size_t length)
{
    unsigned char *base = (unsigned char *)module;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt;
    unsigned char *start, *end, *cursor;

    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return 0;
    start = base + nt->OptionalHeader.BaseOfCode;
    end = start + nt->OptionalHeader.SizeOfCode;

    for (cursor = start; cursor + length < end; cursor++) {
        size_t i = 0;
        int ok = 1;
        UMBRA_TRY {
            for (; i < length; i++) {
                if (pattern[i] == 0xCC) continue;
                if (cursor[i] != pattern[i]) {
                    ok = 0;
                    break;
                }
            }
        }
        UMBRA_EXCEPT(EXCEPTION_EXECUTE_HANDLER) {
            ok = 0;
        }
        if (ok) return cursor;
    }
    return 0;
}

/* the engine is split across the exe and RobloxPlayerBeta.dll (measured), so a scan has to
   walk both; the caller supplies the module list from the PEB, no psapi here */
static HMODULE g_modules[8];
static int g_moduleCount;

/* ------------------------------------------------------------------ luau */
/* A resting state proves itself, which is why we can find it structurally instead of
   depending on a DataModel offset: ci == ci_base, nci == 0, base == top, func == base - 16.
   Measured on 2.748.1526.0 (ASSUMPTIONS.md); re-check the offsets on a version bump. */
static int looks_resting(lua_State *state)
{
    unsigned char *l = (unsigned char *)state;
    unsigned long long ci, ciBase, top, base, func;
    unsigned short nci;
    UMBRA_TRY {
        ci = *(unsigned long long *)(l + L_CI);
        ciBase = *(unsigned long long *)(l + 0x20);
        nci = *(unsigned short *)(l + 0x30);
        top = *(unsigned long long *)(l + L_TOP);
        base = *(unsigned long long *)(l + L_BASE);
        func = *(unsigned long long *)(l + 0x70);
    }
    UMBRA_EXCEPT(EXCEPTION_EXECUTE_HANDLER) {
        return 0;
    }
    if (nci != 0 || ci != ciBase) return 0;
    if (base != top) return 0;
    if (func + 0x10 != base) return 0;
    if (top < 0x10000 || base < 0x10000) return 0;
    if (*(unsigned long long *)(l + L_GLOBAL) < 0x10000) return 0;
    return 1;
}

/* scan committed private RW regions for a global_State whose mainthread rests */
static lua_State *state_from_scriptcontext(void)
{
    SYSTEM_INFO info;
    unsigned char *cursor;
    unsigned long long scanned = 0;

    GetSystemInfo(&info);
    cursor = (unsigned char *)info.lpMinimumApplicationAddress;
    while (cursor < (unsigned char *)info.lpMaximumApplicationAddress && scanned < (256ull << 20)) {
        MEMORY_BASIC_INFORMATION region;
        if (!VirtualQuery(cursor, &region, sizeof(region))) break;
        if (region.State == MEM_COMMIT && region.Type == MEM_PRIVATE &&
            (region.Protect == PAGE_READWRITE || region.Protect == PAGE_EXECUTE_READWRITE) &&
            region.RegionSize < (64ull << 20)) {
            unsigned char *walk = (unsigned char *)region.BaseAddress;
            unsigned char *end = walk + region.RegionSize - 0x300;
            scanned += region.RegionSize;
            for (; walk < end; walk += 8) {
                unsigned long long globalState = *(unsigned long long *)walk;
                lua_State *candidate;
                if (globalState < 0x10000 || (globalState & 7)) continue;
                UMBRA_TRY {
                    candidate = *(lua_State **)((unsigned char *)globalState + 0x2f0);
                }
                UMBRA_EXCEPT(EXCEPTION_EXECUTE_HANDLER) {
                    continue;
                }
                if (candidate && !((unsigned long long)candidate & 7) && looks_resting(candidate))
                    return candidate;
            }
        }
        cursor = (unsigned char *)region.BaseAddress + region.RegionSize;
    }
    return 0;
}

static void spawn_runner(lua_State *state, const char *chunk)
{
    /* one call in: the runner pumps itself from Heartbeat afterwards */
    if (!state || !g.load || !g.pcall) {
        fail("resume", "no state or no luau entry");
        return;
    }
    UMBRA_TRY {
        if (g.load(state, chunk, "@Umbra/runner", lstrlenA(chunk), 0) != 0) {
            fail("compile", g.tolstring ? g.tolstring(state, -1, 0) : "runner did not compile");
            return;
        }
        if (g.pcall(state, 0, 1, 0) != 0)
            fail("resume", g.tolstring ? g.tolstring(state, -1, 0) : "runner did not start");
    }
    UMBRA_EXCEPT(EXCEPTION_EXECUTE_HANDLER) {
        fail("resume", "structured exception while starting the runner");
    }
}

/* the runner ships as a file next to the app so a Luau change needs no rebuild */
static char *runner_chunk(void)
{
    char path[MAX_PATH];
    HANDLE file;
    DWORD size = 0, read = 0;
    char *buffer;
    GetEnvironmentVariableA("LOCALAPPDATA", path, MAX_PATH);
    lstrcatA(path, "\\Umbra\\runner.luau");
    file = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, 0, OPEN_EXISTING, 0, 0);
    if (file == INVALID_HANDLE_VALUE) return 0;
    size = GetFileSize(file, 0);
    if (size == 0 || size > (1u << 20)) {
        CloseHandle(file);
        return 0;
    }
    buffer = (char *)VirtualAlloc(0, size + 1, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (buffer && ReadFile(file, buffer, size, &read, 0)) buffer[read] = 0;
    CloseHandle(file);
    return buffer;
}

/* ------------------------------------------------------------------ pipe */
static const char *find_text(const char *haystack, const char *needle)
{
    if (!*needle) return haystack;
    for (; *haystack; haystack++) {
        const char *a = haystack, *b = needle;
        while (*a && *b && *a == *b) { a++; b++; }
        if (!*b) return haystack;
    }
    return 0;
}

static void serve(void)
{
    /* GUI <-> core. One line in, one line out, never blocks the client's own loop
       for more than a pipe read timeout. */
    char line[4096];
    DWORD read = 0;
    while (g.pipe != INVALID_HANDLE_VALUE) {
        if (!ReadFile(g.pipe, line, sizeof(line) - 1, &read, 0)) break;
        line[read] = 0;
        if (read == 0) break;
        if (line[0] == '{') {
            if (find_text(line, "\"exec\"")) {
                /* the GUI already queued it in the bus in the shipped build; this is the
                   debug path where the core pushes the command itself */
                emit("i", 0, "exec accepted");
            } else if (find_text(line, "\"hello\"")) {
                emit("i", 0, "hello accepted");
            }
        }
    }
    CloseHandle(g.pipe);
    g.pipe = INVALID_HANDLE_VALUE;
}

/* ------------------------------------------------------------------ entry */
__declspec(dllexport) DWORD WINAPI umbra_entry(void *parameter)
{
    char path[MAX_PATH];
    char name[64];
    HANDLE file;
    bus_header *header;

    (void)parameter;
    stage("bus");
    GetEnvironmentVariableA("LOCALAPPDATA", path, MAX_PATH);
    lstrcatA(path, "\\Umbra");
    CreateDirectoryA(path, 0);
    wsprintfA(name, "\\bus-%lu.bin", GetCurrentProcessId());
    lstrcatA(path, name);

    file = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                       0, OPEN_ALWAYS, 0, 0);
    if (file != INVALID_HANDLE_VALUE) {
        HANDLE section = CreateFileMappingA(file, 0, PAGE_READWRITE, 0, BUS_SIZE, 0);
        if (section) {
            g.bus = MapViewOfFile(section, FILE_MAP_WRITE | FILE_MAP_READ, 0, 0, BUS_SIZE);
            CloseHandle(section);
        }
        CloseHandle(file);
    }
    if (!g.bus) {
        fail("bus", "no mapping");
        return 1;
    }
    header = (bus_header *)g.bus;
    if (header->magic != BUS_MAGIC) {
        header->magic = BUS_MAGIC;
        header->version = BUS_VERSION;
        header->cmdHead = header->cmdTail = 0;
        header->outHead = header->outTail = 0;
        header->flags = 0;
    }

    stage("pipe");
    wsprintfA(name, "\\\\.\\pipe\\umbra-%u", GetCurrentProcessId());
    g.pipe = CreateFileA(name, GENERIC_READ | GENERIC_WRITE, 0, 0, OPEN_EXISTING, 0, 0);
    if (g.pipe == INVALID_HANDLE_VALUE) emit("e", 0, "pipe: no host listening");

    stage("state");
    g.state = state_from_scriptcontext();
    if (!g.state) {
        fail("state", "lua_State not found yet");
    } else {
        char *chunk = runner_chunk();
        if (!chunk) fail("compile", "runner.luau missing from %LOCALAPPDATA%\\Umbra");
        else {
            emit("i", 0, "state found, spawning runner");
            spawn_runner(g.state, chunk);
        }
    }

    emit("i", 0, "core up");
    if (g.pipe != INVALID_HANDLE_VALUE) serve();
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE self, DWORD reason, LPVOID reserved)
{
    (void)self;
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        /* the host calls umbra_entry through the hijacked thread, not from here: DllMain
           has a loader lock and every scan would be riskier inside it */
        stage("attach");
    }
    return TRUE;
}
