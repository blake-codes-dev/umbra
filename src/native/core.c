/*  Lyphen core. Manual-mapped, no module entry, no thread of its own.
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
#include <winhttp.h>

/* WinHTTP is not loaded in the client, and a static import cannot be bound by a manual map that
   only resolves already-loaded modules, so the entry points are fetched on first use instead. */
typedef struct {
    HINTERNET (WINAPI *open)(LPCWSTR, DWORD, LPCWSTR, LPCWSTR, DWORD);
    HINTERNET (WINAPI *connect)(HINTERNET, LPCWSTR, INTERNET_PORT, DWORD);
    HINTERNET (WINAPI *open_request)(HINTERNET, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR,
                                     LPCWSTR *, DWORD);
    BOOL (WINAPI *add_headers)(HINTERNET, LPCWSTR, DWORD, DWORD);
    BOOL (WINAPI *send)(HINTERNET, LPCWSTR, DWORD, LPVOID, DWORD, DWORD, DWORD_PTR);
    BOOL (WINAPI *receive)(HINTERNET, LPVOID);
    BOOL (WINAPI *query_headers)(HINTERNET, DWORD, LPCWSTR, LPVOID, LPDWORD, LPDWORD);
    BOOL (WINAPI *read)(HINTERNET, LPVOID, DWORD, LPDWORD);
    BOOL (WINAPI *close)(HINTERNET);
} winhttp_api;

static winhttp_api g_http;
static int g_httpTried;

static int load_http(void)
{
    HMODULE module;
    if (g_http.open) return 1;
    if (g_httpTried) return 0;
    g_httpTried = 1;
    module = LoadLibraryA("winhttp.dll");
    if (!module) return 0;
#define BIND(field, name) *(FARPROC *)&g_http.field = GetProcAddress(module, name)
    BIND(open, "WinHttpOpen");
    BIND(connect, "WinHttpConnect");
    BIND(open_request, "WinHttpOpenRequest");
    BIND(add_headers, "WinHttpAddRequestHeaders");
    BIND(send, "WinHttpSendRequest");
    BIND(receive, "WinHttpReceiveResponse");
    BIND(query_headers, "WinHttpQueryHeaders");
    BIND(read, "WinHttpReadData");
    BIND(close, "WinHttpCloseHandle");
#undef BIND
    return g_http.open && g_http.connect && g_http.open_request && g_http.send &&
           g_http.receive && g_http.query_headers && g_http.read && g_http.close;
}
#include "patterns.h"

/* MSVC has SEH; mingw does not, so a mingw build degrades to plain blocks instead of
   silently losing the guards. Only the MSVC build ships. */
#if defined(__MINGW32__)
#define LYPHEN_TRY if (1)
#define LYPHEN_EXCEPT(e) else
#else
#define LYPHEN_TRY __try
#define LYPHEN_EXCEPT(e) __except (e)
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
#define OUT_ERROR_ID 3

/* measured 2026-11-24 on 2.748.1526.0 (docs: ASSUMPTIONS.md) */
#define L_TOP 0x80
#define L_BASE 0x78
#define L_CI 0x58
#define L_GLOBAL 0x68



#ifndef LYPHEN_HOST
/* in the shipped build there is no lua.h: the state is opaque and the API arrives as
   function pointers from the scan. The harness includes the real header instead. */
typedef void *lua_State;
#endif
typedef int (*luau_load_t)(lua_State *, const char *, const char *, size_t, int);
static lua_State *state_from_file(void);
/* Luau >=0.6 splits the two: compile source to bytecode, then load the bytecode. The bytecode
   buffer is malloc'd by Luau and deliberately leaked here (a few KB, once per inject) because
   freeing it would cross CRTs. */
typedef char *(*luau_compile_t)(const char *, size_t, void *, size_t *);
typedef int (*lua_pcall_t)(lua_State *, int, int, int);
/* Luau's newer ABI takes (L, fn, debugname, nup, cont) and tonumber takes (L, idx, isnum).
   We always pass the full argument list: on Win64 the extra registers are ignored by a
   shorter callee, so this is safe for both variants instead of guessing which one is in the
   client. */
typedef void *(*lua_pushcclosure_t)(lua_State *, void *, const char *, int, void *);
typedef const char *(*lua_tolstring_t)(lua_State *, int, size_t *);
typedef void (*lua_pushlstring_t)(lua_State *, const char *, size_t);
typedef int (*luaB_loadstring_t)(lua_State *);
typedef int (*lua_cfunction_t)(lua_State *);
static const char *top_message(lua_State *, char *, size_t);
typedef void (*lua_pushvfstring_t)(lua_State *, const char *, void *);
typedef void (*lua_createtable_t)(lua_State *, int, int);
typedef void (*lua_setfield_t)(lua_State *, int, const char *);
typedef void (*lua_pushnumber_t)(lua_State *, double);
typedef void (*lua_pushboolean_t)(lua_State *, int);
typedef double (*lua_tonumber_t)(lua_State *, int, int *);
typedef int (*lua_gettop_t)(lua_State *);
typedef void (*lua_rawseti_t)(lua_State *, int, int);

typedef struct {
    unsigned int magic, version, cmdHead, cmdTail, outHead, outTail, flags;
} bus_header;

typedef struct {
    HMODULE exe;
    void *image;
    size_t imageSize;
    lua_State *state;
    luau_load_t load;
    luau_compile_t compile;
    lua_pcall_t pcall;
    lua_pushcclosure_t pushcclosure;
    lua_pushlstring_t pushls;
    luaB_loadstring_t loadstr;
    void *loadstringClosure;      /* heap closure for loadstring, from the gui */
    void *printClosure;           /* heap closure for print, probe only */
    lua_cfunction_t luaBpcall;    /* base library pcall: hook target and callable stand-in for lua_pcall */
    lua_cfunction_t luaBprint;    /* base library print; only ever hooked */
    lua_pushvfstring_t pushvf;
    lua_tolstring_t tolstring;
    lua_createtable_t createtable;
    lua_setfield_t setfield;
    lua_pushnumber_t pushnumber;
    lua_pushboolean_t pushboolean;
    lua_tonumber_t tonumber;
    lua_gettop_t gettop;
    lua_rawseti_t rawseti;
    HANDLE pipe;
    void *bus;
    unsigned int nextId;
} core_state;

static core_state g;
static const char *find_text(const char *haystack, const char *needle);
static void push_bus(lua_State *state);
static void push_bridge(lua_State *state, const char *apiSource);
static int cb_pump(lua_State *state);
static int resolve(void);
static int calibrate(unsigned char *base);
static const char *workspace(char *path);
static unsigned char *next_scan(unsigned char *base, size_t span, unsigned char *from,
                               unsigned char **end);
/* inside a region: on by a page, or out to the region end when it is the last page */
static unsigned char *step_page(unsigned char *page, unsigned char *end)
{
    return page + 0x1000 < end ? page + 0x1000 : end;
}
static int open_pipe(void);
static char g_stage[32] = "start";
static void put(const char *s);
static int g_stopAt, g_stageN;

#ifndef LYPHEN_HOST   /* in the harness the real CRT is linked */
/* ---- the few crt-owned symbols MSVC still references -----------------------------------------
   Linking the static CRT would drag its startup in (cookies, per-thread data, atexit) and the
   payload is manual-mapped with no init call, so the handful of symbols the compiler and the
   SEH dispatcher emit are provided here instead. The CFG dispatch pointers become a two byte
   `jmp rax` thunk built once at load; the "is this image data read-only" query walks our own
   section table. */
int _fltused = 0;
int __isa_available = 1;      /* x64 always has SSE2; the crt's mem* dispatch reads this */
unsigned int __favor = 0;     /* no os-enabled extras to favour */
size_t __memset_fast_string_threshold = 0x8000;
size_t __memset_nt_threshold = 0x100000;
void *__guard_check_icall_fptr;
void *__guard_dispatch_icall_fptr;

#pragma function(memcpy, memset, memcmp)
void *calloc(size_t count, size_t size)
{
    size_t total = count * size;
    return HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, total ? total : 1);
}

void *malloc(size_t size)
{
    return HeapAlloc(GetProcessHeap(), 0, size ? size : 1);
}

void free(void *block)
{
    if (block) HeapFree(GetProcessHeap(), 0, block);
}

#pragma function(strlen)
size_t strlen(const char *text)
{
    const char *p = text;
    while (*p) p++;
    return (size_t)(p - text);
}

int strcpy_s(char *to, size_t size, const char *from)
{
    size_t i = 0;
    if (!size) return 22;
    while (from[i] && i + 1 < size) { to[i] = from[i]; i++; }
    to[i] = 0;
    return 0;
}

int wcsncmp(const wchar_t *a, const wchar_t *b, size_t count)
{
    while (count--) { if (*a != *b) return *a - *b; if (!*a) break; a++; b++; }
    return 0;
}

void abort(void) { ExitProcess(3); }
void terminate(void) { ExitProcess(3); }

int _IsNonwritableInCurrentImage(const void *target)
{
    unsigned char *base = (unsigned char *)GetModuleHandleA(0);
    unsigned char *nt;
    unsigned short sections;
    int i;
    if (!base) return 0;
    nt = base + *(unsigned int *)(base + 0x3C);
    sections = *(unsigned short *)(nt + 6);
    nt = nt + 24 + *(unsigned short *)(nt + 20);
    for (i = 0; i < sections; i++) {
        unsigned char *entry = nt + i * 40;
        unsigned char *start = base + *(unsigned int *)(entry + 12);
        unsigned char *end = start + *(unsigned int *)(entry + 8);
        const unsigned char *p = (const unsigned char *)((unsigned long long)target & ~3ull);
        if (p >= start && p < end) return (*(unsigned int *)(entry + 36) & 0x80000000u) != 0;
    }
    return 0;
}

static void init_guard_thunks(void)
{
    unsigned char *thunk = (unsigned char *)VirtualAlloc(0, 0x1000, MEM_COMMIT | MEM_RESERVE,
                                                       PAGE_EXECUTE_READWRITE);
    if (!thunk) return;
    thunk[0] = 0xFF;                     /* jmp rax - the caller leaves the target in rax */
    thunk[1] = 0xE0;
    thunk[2] = 0xC3;                     /* the check-style call just returns */
    __guard_check_icall_fptr = (void *)(thunk + 2);
    __guard_dispatch_icall_fptr = (void *)thunk;
    FlushInstructionCache(GetCurrentProcess(), thunk, 8);
}
#endif

/* ------------------------------------------------------------------ plumbing */
/* safe read: MSVC uses SEH, mingw (whose SEH is a no-op) reads through the kernel instead.
   Everything that can point at a wrong address goes through this. */
static int decrypt_touch(const void *at);
static int armed_page(const void *at, size_t size);

static int safe_read(const void *at, void *out, size_t size)
{
#if defined(LYPHEN_HOST)
    /* the harness owns its own memory, so reads can be direct - but the image has uncommitted
       holes, so the region is checked first. The last region is cached because the scanners hit
       the same page millions of times. */
    {
        static const unsigned char *cacheBase, *cacheEnd;
        MEMORY_BASIC_INFORMATION region;
        const unsigned char *p = (const unsigned char *)at;
        if (!cacheBase || p < cacheBase || p + size > cacheEnd) {
            if (!VirtualQuery(at, &region, sizeof(region))) return 0;
            if (region.State != MEM_COMMIT) return 0;
            cacheBase = (const unsigned char *)region.BaseAddress;
            cacheEnd = cacheBase + region.RegionSize;
        }
        if (p + size > cacheEnd) return 0;
        memcpy(out, at, size);
        return 1;
    }
#elif defined(__MINGW32__)
    SIZE_T got = 0;
    return ReadProcessMemory(GetCurrentProcess(), at, out, size, &got) && got == size;
#else
    /* No arming first. A guard page has to fault - Hyperion's vectored handler decrypts the page
       and resumes us - so a guard is passed on (CONTINUE_SEARCH) and only a real access violation
       (nothing backed there at all) is treated as unreadable. Clearing the guard ourselves would
       skip the decrypt and hand back ciphertext. */
    LYPHEN_TRY {
        const unsigned char *from = (const unsigned char *)at;
        unsigned char *to = (unsigned char *)out;
        for (size_t i = 0; i < size; i++) to[i] = from[i];
        return 1;
    }
    LYPHEN_EXCEPT(GetExceptionCode() == 0x80000001u ? EXCEPTION_CONTINUE_SEARCH
                                                    : EXCEPTION_EXECUTE_HANDLER) {
        return 0;
    }
#endif
}

static void stage(const char *name)
{
    int i = 0;
    while (name[i] && i < 31) {
        g_stage[i] = name[i];
        i++;
    }
    g_stage[i] = 0;
    /* diagnostics: stop.txt holds a stage number and the entry hands the thread back there, which
       bisects "the client dies after the entry" stage by stage */
    if (g_stopAt > 0 && ++g_stageN >= g_stopAt) {
        put("stop hit");
        ExitThread(0);
    }
}

static void put(const char *s)
{
    /* last resort debug channel: only used while the pipe is not up yet */
    HANDLE f = CreateFileA("C:\\Windows\\Temp\\lyphen-core.log", FILE_APPEND_DATA,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, 0, OPEN_ALWAYS, 0, 0);
    DWORD w;
#ifdef LYPHEN_HOST
    { extern int printf(const char *fmt, ...); extern char *getenv(const char *);
      if (getenv("LYPHEN_STAGES")) printf("  [stage] %s\n", s); }
#endif
    if (f != INVALID_HANDLE_VALUE) {
        WriteFile(f, s, lstrlenA(s), &w, 0);
        WriteFile(f, "\r\n", 2, &w, 0);
        CloseHandle(f);
    }
}

static void emit(const char *kind, unsigned int id, const char *text);

/* a failure is tagged with the stage it happened in, so one run is diagnosable */
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

static void pipe_line(const char *text)
{
    DWORD written = 0;
    if (g.pipe == INVALID_HANDLE_VALUE || !text) return;
    WriteFile(g.pipe, text, lstrlenA(text), &written, 0);
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
        unsigned char window[64];
        int ok = 1;
        if (length > sizeof(window)) continue;
        if (!safe_read(cursor, window, length)) continue;
        for (; i < length; i++) {
            if (pattern[i] == 0xCC) continue;
            if (window[i] != pattern[i]) {
                ok = 0;
                break;
            }
        }
        if (ok) return cursor;
    }
    return 0;
}

/* the module list comes from the PEB in module_base(); no psapi, no exports */

/* ------------------------------------------------------------------ luau */
/* A resting state proves itself, which is why we can find it structurally instead of
   depending on a DataModel offset: ci == ci_base, nci == 0, base == top, func == base - 16.
   Measured on 2.748.1526.0 (ASSUMPTIONS.md); re-check the offsets on a version bump. */
static int looks_resting(lua_State *state)
{
    unsigned char header[0x100];
    unsigned long long ci, ciBase, top, base, func, global;
    unsigned short nci;
    if (!safe_read(state, header, sizeof(header))) return 0;
    ci = *(unsigned long long *)(header + L_CI);
    ciBase = *(unsigned long long *)(header + 0x20);
    nci = *(unsigned short *)(header + 0x30);
    top = *(unsigned long long *)(header + L_TOP);
    base = *(unsigned long long *)(header + L_BASE);
    func = *(unsigned long long *)(header + 0x70);
    global = *(unsigned long long *)(header + L_GLOBAL);
    if (nci != 0 || ci != ciBase) return 0;
    if (base != top) return 0;
    if (func + 0x10 != base) return 0;
    if (top < 0x10000 || base < 0x10000) return 0;
    if (global < 0x10000) return 0;
    return 1;
}

/* scan committed private RW regions for a global_State whose mainthread rests */
/* Cheap validity window for a deref: one VirtualQuery per new page instead of one per probe.
   The walk itself only touches the region it is currently inside, so nothing here can fault on
   a boundary or on a region smaller than the headroom it assumes. */
static int window_ok(void *at, size_t size, unsigned char **winBase, unsigned char **winEnd)
{
    MEMORY_BASIC_INFORMATION region;
    unsigned char *p = (unsigned char *)at;
    if (*winBase && p >= *winBase && p + size <= *winEnd) return 1;
    *winBase = 0;
    if (!VirtualQuery(at, &region, sizeof(region))) return 0;
    if (region.State != MEM_COMMIT) return 0;
    if (!(region.Protect & (PAGE_READWRITE | PAGE_EXECUTE_READWRITE | PAGE_READONLY |
                            PAGE_WRITECOPY | PAGE_EXECUTE_READ | PAGE_EXECUTE)))
        return 0;
    if (region.Protect & PAGE_GUARD) armed_page(p, size);
    *winBase = (unsigned char *)region.BaseAddress;
    *winEnd = *winBase + region.RegionSize;
    return p + size <= *winEnd;
}

/* Finds the client's lua_State by walking private heap for a global_State pointer. Every
   dereference is bounded: an 8 GB private heap holds plenty of 8-aligned junk. */
static lua_State *state_from_scriptcontext(void)
{
    SYSTEM_INFO info;
    unsigned char *cursor;
    unsigned char *winBase = 0, *winEnd = 0;
    unsigned long long scanned = 0, reads = 0;

    GetSystemInfo(&info);
    cursor = (unsigned char *)info.lpMinimumApplicationAddress;
    while (cursor < (unsigned char *)info.lpMaximumApplicationAddress &&
           scanned < (512ull << 20) && reads < 6000000ull) {
        MEMORY_BASIC_INFORMATION region;
        if (!VirtualQuery(cursor, &region, sizeof(region))) break;
        cursor = (unsigned char *)region.BaseAddress + region.RegionSize;
        if (region.State != MEM_COMMIT || region.Type != MEM_PRIVATE) continue;
        if (!(region.Protect & (PAGE_READWRITE | PAGE_EXECUTE_READWRITE))) continue;
        if (region.RegionSize < 0x400 || region.RegionSize > (64ull << 20)) continue;
        {
            unsigned char *walk = (unsigned char *)region.BaseAddress;
            unsigned char *end = walk + region.RegionSize - 0x300;
            unsigned char *pageMark = 0;
            scanned += region.RegionSize;
            for (; walk < end; walk += 8, reads++) {
                unsigned long long globalState;
                if ((unsigned char *)((ULONG_PTR)walk & ~(ULONG_PTR)0xFFF) != pageMark) {
                    pageMark = (unsigned char *)((ULONG_PTR)walk & ~(ULONG_PTR)0xFFF);
                    armed_page(pageMark, 8);
                }
                if (!safe_read(walk, &globalState, 8)) continue;
                unsigned long long slot;
                void *candidate = 0;
                if (globalState < 0x10000 || (globalState & 7)) continue;
                if (!window_ok((void *)((ULONG_PTR)globalState + 0x2f0), 8, &winBase, &winEnd))
                    continue;
                slot = (ULONG_PTR)globalState + 0x2f0;
                if (!safe_read((void *)slot, &candidate, 8)) continue;
                if (!candidate || ((ULONG_PTR)candidate & 7)) continue;
                if (!window_ok((void *)candidate, 0x110, &winBase, &winEnd))
                    continue;
                if (looks_resting((lua_State *)candidate)) return (lua_State *)candidate;
            }
        }
    }
    {
        char line[96];
        wsprintfA(line, "state scan read %lu MB", (unsigned long)(scanned >> 20));
        put(line);
    }
    return 0;
}

/* A pushed Lua string without lua_pushlstring: the value is a plain TValue on the stack and for a
   string that is just a pointer plus tag 5, so the only thing missing is an object with the
   TString shape lua_tolstring reads - next, flags, length at +0x14, bytes at +0x18. A local
   buffer of ours is enough for a value that only has to survive the call; two slots so a source
   and a chunkname can be live at once. */
static unsigned char g_fakeString[2][0x18 + 0x1000];
static int g_fakeSlot;

static void push_value(lua_State *state, void *pointer, unsigned int tag)
{
    unsigned char *top = *(unsigned char **)((unsigned char *)state + L_TOP);
    *(void **)top = pointer;
    *(unsigned int *)(top + 0xC) = tag;
    *(unsigned char **)((unsigned char *)state + L_TOP) = top + 0x10;
}

static unsigned char *make_string(const char *text)
{
    size_t length = lstrlenA(text);
    unsigned char *object = g_fakeString[g_fakeSlot++ & 1];
    if (length > 0xFF0) length = 0xFF0;
    memset(object, 0, 0x18);
    *(unsigned int *)(object + 8) = 0x00000501;   /* flags as the client lays them out */
    *(unsigned int *)(object + 0x14) = (unsigned int)length;
    memcpy(object + 0x18, text, length);
    object[0x18 + length] = 0;
    return object;
}

static void push_string(lua_State *state, const char *text)
{
    push_value(state, make_string(text), 5);
}

/* tag of the value on top; a C function returning LUA_MULTRET says nothing about results, so
   the result is judged by what is actually on the stack */
static unsigned int top_tag(lua_State *state)
{
    unsigned char *top = *(unsigned char **)((unsigned char *)state + L_TOP);
    return *(unsigned int *)(top - 0x10 + 0xC);
}

/* Calling a base-library function from a resting state: pcall counts its arguments as
   `top - ci->func - 1`, and ci->func is the frame slot one below base, so the function has to be
   parked there. That slot belongs to the frame we interrupted, so it is saved and put back after
   the call, and the results are dropped by restoring top. */
static int call_borrowing_frame(lua_State *state, void *function, const char **args, int count,
                                char *message, size_t messageSize)
{
    unsigned char *base = *(unsigned char **)((unsigned char *)state + L_BASE);
    unsigned char *slot = base - 0x10;
    unsigned char saved[0x10];
    int status;
    memcpy(saved, slot, sizeof(saved));
    *(void **)slot = function;
    *(unsigned int *)(slot + 0xC) = 7;
    for (int i = 0; i < count; i++) push_string(state, args[i]);
    {
        unsigned char *before = *(unsigned char **)((unsigned char *)state + L_TOP);
        char line[64];
        wsprintfA(line, "call %p nargs %d base-top %d", function, count,
                  (int)((before - base) / 0x10));
        put(line);
    }
    status = g.luaBpcall(state);
    if (status == 2 && message) top_message(state, message, messageSize);
    {
        unsigned char *after = *(unsigned char **)((unsigned char *)state + L_TOP);
        char line[64];
        wsprintfA(line, "call ret %d top-base-after %d tag %u", status,
                  (int)((after - base) / 0x10), *(unsigned int *)(after - 0x10 + 0xC));
        put(line);
    }
    memcpy(slot, saved, sizeof(saved));
    *(unsigned char **)((unsigned char *)state + L_TOP) = base;
    return status;
}

/* the message from a failed call, read straight off the string object */
static const char *top_message(lua_State *state, char *out, size_t size)
{
    unsigned char *top = *(unsigned char **)((unsigned char *)state + L_TOP);
    unsigned char *value = top - 0x10;
    if (*(unsigned int *)(value + 0xC) != 5) return "no message";
    {
        unsigned char *object = *(unsigned char **)value;
        return safe_read(object + 0x18, out, size - 1) ? (out[size - 1] = 0, out) : "unreadable";
    }
}

/* source -> bytecode -> closure. Returns the same codes as luau_load so callers stay simple. */
/* Client calls are made inside this so an api_check throw or an AV is reported against the stage
   instead of taking the process down. The unwinder needs our table for this to be reachable at
   all, which is why register_unwind() runs first. */
static int g_lastCallCode;

#define GUARDED(statement)                                                       \
    (g_lastCallCode = 0,                                                         \
     LYPHEN_TRY { (void)(statement); }                                           \
     LYPHEN_EXCEPT(EXCEPTION_EXECUTE_HANDLER) {                                  \
         g_lastCallCode = (int)(unsigned)GetExceptionCode();                     \
         put("call guarded");                                                    \
     },                                                                          \
     g_lastCallCode)

static int load_source(lua_State *state, const char *chunkname, const char *source)
{
    size_t size = 0;
    char *bytecode;
    int status;
    if (!g.load && g.loadstr && g.pushvf) {
        /* no luau_load measured: drive the base library's own loadstring, which reads the source
           and the optional chunkname off the stack and leaves closure or nil+message */
        push_string(state, source);
        if (chunkname) push_string(state, chunkname);
        status = g.loadstr(state);
        return status == 1 ? 0 : 1;
    }
    if (!g.load) return 1;
    if (!g.compile) {
        /* luau_load without a compiler: it re-parses source when the buffer is not bytecode,
           which is what costs a bytecode cache in production and nothing today */
        return g.load(state, chunkname, source, lstrlenA(source), 0);
    }
    bytecode = g.compile(source, lstrlenA(source), 0, &size);
    if (!bytecode || size == 0) return -1;
    status = g.load(state, chunkname, bytecode, size, 0);
    return status;
}

static void spawn_runner(lua_State *state, char *apiChunk, char *chunk)
{
    /* One native call in; the runner pumps itself from Heartbeat afterwards.
         push bridge (with the api source inside) -> [bridge]
         load runner                             -> [bridge, runner]
         push bus                                -> [bridge, runner, bus]
         pcall(2)                                -> runner(bridge, bus)
       The runner compiles api.luau through the bridge, so nothing has to be passed as a
       second call argument: an api table pushed before the runner would sit below it and
       pcall would try to call the table (which is exactly what the harness caught). */
    if (!state || !g.pcall || (!g.load && !(g.loadstr && g.pushvf))) {
        fail("resume", "no state or no luau entry");
        return;
    }
    if (!g.createtable || !g.pushcclosure || !g.setfield || !g.rawseti) {
        fail("resume", "table api unresolved (bridges cannot be built)");
        return;
    }
    if (!chunk) {
        fail("compile", "runner.luau missing");
    if (!g.state) {
        g.state = state_from_file();
        if (g.state) put("sfile hit");
    }
        return;
    }
#ifdef UMBRA_HOST
    { extern int printf(const char *fmt, ...); printf("  [dbg] sr state=%p load=%p compile=%p api=%p src=%p\n",
        (void*)state,(void*)g.load,(void*)g.compile,(void*)apiChunk,(void*)chunk); }
#endif
    {
        /* function first, then its arguments: pcall takes the callee from
           L->top - (nargs + 1), so anything pushed before the callee would be called instead.
           stack: [runner] [bridge] [bus] -> pcall(2) -> runner(bridge, bus) */
        if (load_source(state, "@Lyphen/runner", chunk) != 0) {
            fail("compile", g.tolstring ? g.tolstring(state, -1, 0) : "runner did not compile");
            return;
        }
        push_bridge(state, apiChunk);
        push_bus(state);
        if (g.pcall(state, 2, 0, 0) != 0)
            fail("resume", g.tolstring ? g.tolstring(state, -1, 0) : "runner did not start");
    }
}

/* ------------------------------------------------------------------ bridges
 * Every bridge is a C closure taking its arguments off the stack by index and pushing one
 * result. They never throw across the C boundary: a failure is a pushed nil + message, so a
 * broken path degrades one API function instead of unwinding through the runner.
 */
static int argstr(lua_State *state, int index, char *out, size_t size)
{
    size_t length = 0;
    const char *text = g.tolstring ? g.tolstring(state, index, &length) : 0;
    if (!text) return 0;
    if (length >= size) length = size - 1;
    for (size_t i = 0; i < length; i++) out[i] = text[i];
    out[length] = 0;
    return 1;
}

static const char *workspace(char *path)
{
    GetEnvironmentVariableA("LOCALAPPDATA", path, MAX_PATH);
    lstrcatA(path, "\\Lyphen\\workspace");
    CreateDirectoryA(path, 0);
    return path;
}

/* "a/b.lua" -> <workspace>\a\b.lua, refusing any ".." segment */
static int resolve_path(char *path)
{
    char base[MAX_PATH];
    char rel[MAX_PATH];
    const char *p;
    workspace(base);
    p = path;
    if (!*p || p[0] == '/' || p[0] == '\\' || (p[0] && p[1] == ':')) return 0;
    if (lstrlenA(p) > 200) return 0;
    lstrcpyA(rel, p);
    for (char *c = rel; *c; c++) {
        if (*c == '/') *c = '\\';
    }
    if (find_text(rel, "..")) return 0;
    lstrcatA(base, "\\");
    lstrcatA(base, rel);
    for (char *c = base; *c; c++) if (*c == '/') *c = '\\';
    lstrcpyA(path, base);
    return 1;
}

static void failarg(lua_State *state, const char *message)
{
    if (g.pushboolean) g.pushboolean(state, 0);
    if (g.pushls) g.pushls(state, message, lstrlenA(message));
}

/* create each missing directory on the way to the file */
static void make_parents(const char *full)
{
    char partial[MAX_PATH];
    lstrcpyA(partial, full);
    for (char *c = partial; *c; c++) {
        if (*c != '\\') continue;
        *c = 0;
        if (lstrlenA(partial) > 3) CreateDirectoryA(partial, 0);
        *c = '\\';
    }
}

static int bridge_write(lua_State *state, int append)
{
    char path[MAX_PATH];
    char text[8192];
    HANDLE file;
    DWORD written = 0;
    if (!argstr(state, 1, path, MAX_PATH) || !resolve_path(path)) return failarg(state, "bad path"), 2;
    if (!argstr(state, 2, text, sizeof(text))) return failarg(state, "bad body"), 2;
    make_parents(path);
    file = CreateFileA(path, append ? FILE_APPEND_DATA : GENERIC_WRITE, 0, 0,
                       append ? OPEN_ALWAYS : CREATE_ALWAYS, 0, 0);
    if (file == INVALID_HANDLE_VALUE) return failarg(state, "open failed"), 2;
    WriteFile(file, text, lstrlenA(text), &written, 0);
    CloseHandle(file);
    if (g.pushboolean) g.pushboolean(state, 1);
    return 1;
}

static int cb_writefile(lua_State *state) { return bridge_write(state, 0); }
static int cb_appendfile(lua_State *state) { return bridge_write(state, 1); }

static int cb_readfile(lua_State *state)
{
    char path[MAX_PATH];
    HANDLE file;
    DWORD size, read = 0;
    char *buffer;
    if (!argstr(state, 1, path, MAX_PATH) || !resolve_path(path)) return failarg(state, "bad path"), 2;
    file = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, 0, OPEN_EXISTING, 0, 0);
    if (file == INVALID_HANDLE_VALUE) return failarg(state, "not found"), 2;
    size = GetFileSize(file, 0);
    if (size > (4u << 20)) { CloseHandle(file); return failarg(state, "too large"), 2; }
    buffer = (char *)VirtualAlloc(0, size + 1, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!buffer) { CloseHandle(file); return failarg(state, "no memory"), 2; }
    ReadFile(file, buffer, size, &read, 0);
    CloseHandle(file);
    if (g.pushls) g.pushls(state, buffer, read);
    VirtualFree(buffer, 0, MEM_RELEASE);
    return 1;
}

static int cb_isfile(lua_State *state)
{
    char path[MAX_PATH];
    HANDLE file;
    if (!argstr(state, 1, path, MAX_PATH) || !resolve_path(path)) return failarg(state, "bad path"), 2;
    file = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, 0, OPEN_EXISTING, 0, 0);
    if (g.pushboolean) g.pushboolean(state, file != INVALID_HANDLE_VALUE);
    if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
    return 1;
}

static int cb_delfile(lua_State *state)
{
    char path[MAX_PATH];
    if (!argstr(state, 1, path, MAX_PATH) || !resolve_path(path)) return failarg(state, "bad path"), 2;
    if (g.pushboolean) g.pushboolean(state, DeleteFileA(path) != 0);
    return 1;
}

static int cb_makefolder(lua_State *state)
{
    char path[MAX_PATH];
    if (!argstr(state, 1, path, MAX_PATH) || !resolve_path(path)) return failarg(state, "bad path"), 2;
    make_parents(path);
    if (g.pushboolean) g.pushboolean(state, CreateDirectoryA(path, 0) != 0);
    return 1;
}

static int cb_isfolder(lua_State *state)
{
    char path[MAX_PATH];
    DWORD attributes;
    if (!argstr(state, 1, path, MAX_PATH) || !resolve_path(path)) return failarg(state, "bad path"), 2;
    attributes = GetFileAttributesA(path);
    if (g.pushboolean) g.pushboolean(state, attributes != INVALID_FILE_ATTRIBUTES &&
                                     (attributes & FILE_ATTRIBUTE_DIRECTORY));
    return 1;
}

/* listfiles: returns a table array of joined names, one level, folders suffixed with '/' */
static int cb_listfiles(lua_State *state)
{
    char path[MAX_PATH];
    char pattern[MAX_PATH];
    WIN32_FIND_DATAA data;
    HANDLE search;
    int index = 0;
    if (!argstr(state, 1, path, MAX_PATH)) return failarg(state, "bad path"), 2;
    if (!*path) lstrcpyA(path, ".");
    if (!resolve_path(path)) return failarg(state, "bad path"), 2;
    lstrcpyA(pattern, path);
    lstrcatA(pattern, "\\*");
    if (g.createtable) g.createtable(state, 0, 0);
    search = FindFirstFileA(pattern, &data);
    if (search != INVALID_HANDLE_VALUE) {
        do {
            char name[MAX_PATH];
            if (data.cFileName[0] == '.' && !data.cFileName[1]) continue;
            if (data.cFileName[0] == '.' && data.cFileName[1] == '.' && !data.cFileName[2]) continue;
            lstrcpyA(name, data.cFileName);
            if (data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) lstrcatA(name, "\\");
            if (g.pushls) g.pushls(state, name, lstrlenA(name));
            if (g.rawseti) g.rawseti(state, -2, index + 1);
            index++;
        } while (FindNextFileA(search, &data));
        FindClose(search);
    }
    return 1;
}


/* ------------------------------------------------------------------ http
 * WinHTTP, positional args: (url, method, body, contentType) -> {status, body, headers}.
 * Never throws on an HTTP status: the caller decides what a 403 means.
 */
static int cb_request(lua_State *state)
{
    char url[2048], method[8], body[8192], type[128];
    wchar_t wideUrl[2048], wideType[128], wideMethod[8];
    HINTERNET session, connect, request;
    INTERNET_PORT port = 443;
    const char *cursor;
    char host[256], path[1024];
    int secure = 1, status = 0, offset = 0;
    DWORD read = 0;
    char *chunk;

    if (!argstr(state, 1, url, sizeof(url))) return failarg(state, "bad url"), 3;
    if (!argstr(state, 2, method, sizeof(method))) lstrcpyA(method, "GET");
    if (!g.tolstring || !g.tolstring(state, 3, 0)) body[0] = 0;
    else argstr(state, 3, body, sizeof(body));
    if (!g.tolstring || !g.tolstring(state, 4, 0)) type[0] = 0;
    else argstr(state, 4, type, sizeof(type));

    cursor = url;
    if (cursor[0] == 'h' && cursor[4] == ':' ) cursor += 7;   /* https:// */
    else if (cursor[0] == 'h' && cursor[3] == ':') { cursor += 6; secure = 0; port = 80; }
    else return failarg(state, "only http(s)"), 3;

    while (*cursor && *cursor != '/' && *cursor != ':' && offset < 255) host[offset++] = *cursor++;
    host[offset] = 0;
    offset = 0;
    if (*cursor == ':') { port = 0; cursor++; while (*cursor >= '0' && *cursor <= '9') port = port * 10 + (*cursor++ - '0'); }
    if (*cursor != '/') path[offset++] = '/';
    while (*cursor && offset < 1000) path[offset++] = *cursor++;
    path[offset] = 0;

    MultiByteToWideChar(CP_ACP, 0, method, -1, wideMethod, 8);
    MultiByteToWideChar(CP_ACP, 0, host, -1, wideUrl, 2048);
    if (type[0]) MultiByteToWideChar(CP_ACP, 0, type, -1, wideType, 128);

    if (!load_http()) { failarg(state, "no winhttp"); return 0; }
    session = g_http.open(L"Lyphen/0.1", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
                          WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session) return failarg(state, "winhttp open"), 3;
    connect = g_http.connect(session, wideUrl, port, 0);
    if (!connect) { g_http.close(session); return failarg(state, "connect"), 3; }

    {   wchar_t widePath[1024];
        MultiByteToWideChar(CP_ACP, 0, path, -1, widePath, 1024);
        request = g_http.open_request(connect, wideMethod, widePath, 0,
                                     WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                     secure ? WINHTTP_FLAG_SECURE : 0);
    }
    if (!request) {
        g_http.close(connect); g_http.close(session);
        return failarg(state, "request"), 3;
    }
    if (type[0]) g_http.add_headers(request, wideType, -1, WINHTTP_ADDREQ_FLAG_ADD);

    if (g_http.send(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                           body[0] ? body : WINHTTP_NO_REQUEST_DATA, lstrlenA(body), lstrlenA(body), 0) &&
        g_http.receive(request, 0)) {
        DWORD code = 0, size = sizeof(code);
        g_http.query_headers(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                            WINHTTP_HEADER_NAME_BY_INDEX, &code, &size, WINHTTP_NO_HEADER_INDEX);
        status = (int)code;
    }

    if (g.createtable) g.createtable(state, 0, 3);
    if (g.pushnumber) g.pushnumber(state, (double)status);
    if (g.setfield) g.setfield(state, -2, "status");

    if (g.pushls) {
        /* body is appended in chunks into one pushed string */
        char *all = (char *)VirtualAlloc(0, (1u << 20), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        DWORD total = 0;
        while (all && total < (1u << 20) - 4096) {
            chunk = all + total;
            if (!g_http.read(request, chunk, 4096, &read) || read == 0) break;
            total += read;
        }
        g.pushls(state, all ? all : "", all ? total : 0);
        if (all) VirtualFree(all, 0, MEM_RELEASE);
    }
    if (g.setfield) g.setfield(state, -2, "body");

    g_http.close(request);
    g_http.close(connect);
    g_http.close(session);
    return 1;
}


/* ------------------------------------------------------------------ input */
static int cb_mousemoverel(lua_State *state)
{
    double dx = g.tonumber ? g.tonumber(state, 1, 0) : 0;
    double dy = g.tonumber ? g.tonumber(state, 2, 0) : 0;
    INPUT input;
    input.type = INPUT_MOUSE;
    input.mi.dx = (LONG)dx;
    input.mi.dy = (LONG)dy;
    input.mi.dwFlags = MOUSEEVENTF_MOVE;
    input.mi.time = 0;
    input.mi.dwExtraInfo = 0;
    input.mi.mouseData = 0;
    SendInput(1, &input, sizeof(input));
    return 0;
}

/* a single printable character goes through VkKeyScan; a named key through the table */
static unsigned short virtual_key(const char *name)
{
    static const struct { const char *name; unsigned short code; } keys[] = {
        {"enter", 0x0D}, {"return", 0x0D}, {"tab", 0x09}, {"space", 0x20},
        {"escape", 0x1B}, {"backspace", 0x08}, {"shift", 0x10}, {"control", 0x11},
        {"ctrl", 0x11}, {"alt", 0x12}, {"up", 0x26}, {"down", 0x28}, {"left", 0x25},
        {"right", 0x27}, {"delete", 0x2E}, {"insert", 0x2D}, {"home", 0x24}, {"end", 0x23},
        {"pageup", 0x21}, {"pagedown", 0x22}, {"f1", 0x70}, {"f2", 0x71}, {"f3", 0x72},
        {"f4", 0x73}, {"f5", 0x74}, {"f6", 0x75}, {0, 0}
    };
    char lower[16];
    int i = 0;
    while (name[i] && i < 15) { lower[i] = (char)(name[i] >= 'A' && name[i] <= 'Z' ? name[i] + 32 : name[i]); i++; }
    lower[i] = 0;
    for (i = 0; keys[i].name; i++) if (!lstrcmpA(keys[i].name, lower)) return keys[i].code;
    if (lstrlenA(lower) == 1) {
        short scan = VkKeyScanA(lower[0]);
        if (scan != -1) return (unsigned short)(scan & 0xFF);
    }
    return 0;
}

static int bridge_key(lua_State *state, int up)
{
    char name[32];
    unsigned short code;
    INPUT input;
    if (!argstr(state, 1, name, sizeof(name))) return 0;
    code = virtual_key(name);
    if (!code) return 0;
    input.type = INPUT_KEYBOARD;
    input.ki.wVk = code;
    input.ki.wScan = (WORD)MapVirtualKeyA(code, 0);
    input.ki.dwFlags = up ? KEYEVENTF_KEYUP : 0;
    input.ki.time = 0;
    input.ki.dwExtraInfo = 0;
    SendInput(1, &input, sizeof(input));
    return 0;
}

static int cb_keypress(lua_State *state) { return bridge_key(state, 0); }
static int cb_keyrelease(lua_State *state) { return bridge_key(state, 1); }

static int cb_setclipboard(lua_State *state)
{
    char text[8192];
    wchar_t wide[8192];
    HGLOBAL block;
    void *locked;
    int length;
    if (!argstr(state, 1, text, sizeof(text))) return 0;
    length = MultiByteToWideChar(CP_UTF8, 0, text, -1, wide, 8192);
    block = GlobalAlloc(GMEM_MOVEABLE, (SIZE_T)length * 2);
    if (!block) return 0;
    locked = GlobalLock(block);
    if (locked) {
        CopyMemory(locked, wide, (SIZE_T)length * 2);
        GlobalUnlock(block);
        if (OpenClipboard(0)) {
            EmptyClipboard();
            SetClipboardData(CF_UNICODETEXT, block);
            CloseClipboard();
            return 0;
        }
    }
    GlobalFree(block);
    return 0;
}

/* chunk loader for Luau: (source) -> function | nil, err */
static int cb_loadstring(lua_State *state)
{
    char *source;
    size_t size = 0;
    char *bytecode;
    size_t length = 0;
    const char *text = g.tolstring ? g.tolstring(state, 1, &length) : 0;
    if (!text || !g.compile || (!g.load && !(g.loadstr && g.pushvf)))
        return failarg(state, "loadstring unavailable"), 2;
    source = (char *)VirtualAlloc(0, length + 1, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!source) return failarg(state, "no memory"), 2;
    for (size_t i = 0; i < length; i++) source[i] = text[i];
    source[length] = 0;
    bytecode = g.compile(source, length, 0, &size);
    VirtualFree(source, 0, MEM_RELEASE);
    if (!bytecode || size == 0) return failarg(state, "compile failed"), 2;
    if (g.load(state, "@Lyphen/chunk", bytecode, size, 0) != 0) return 1;   /* nil, err */
    return 1;
}

static int cb_windowactive(lua_State *state)
{
    HWND foreground = GetForegroundWindow();
    DWORD pid = 0;
    if (foreground) GetWindowThreadProcessId(foreground, &pid);
    if (g.pushboolean) g.pushboolean(state, pid == GetCurrentProcessId());
    return 1;
}


/* ------------------------------------------------------------------ bus table
 * The runner's only view of the shared memory. take() hands it a batch and advances the
 * tail, push() writes one output slot. Both are plain closures, no state beyond `g.bus`.
 */
static int cb_bus_push(lua_State *state)
{
    bus_header *header = (bus_header *)g.bus;
    char *slot;
    char text[512];
    unsigned int kind = g.tonumber ? (unsigned int)g.tonumber(state, 1, 0) : 4;
    unsigned int id = g.tonumber ? (unsigned int)g.tonumber(state, 2, 0) : 0;
    unsigned int length;
    if (!header) return 0;
    if (!argstr(state, 3, text, sizeof(text))) text[0] = 0;
    length = (unsigned int)lstrlenA(text);
    slot = (char *)g.bus + OUT_BASE + (header->outHead % OUT_SLOTS) * OUT_SIZE;
    *(unsigned int *)(slot + 0) = kind;
    *(unsigned int *)(slot + 4) = id;
    *(unsigned int *)(slot + 8) = length;
    for (unsigned int i = 0; i < length; i++) slot[12 + i] = text[i];
    header->outHead = header->outHead + 1;
    return 0;
}

static int cb_bus_take(lua_State *state)
{
    bus_header *header = (bus_header *)g.bus;
    unsigned int limit = g.tonumber ? (unsigned int)g.tonumber(state, 1, 0) : 8;
    unsigned int count = 0;
    if (!header || !g.createtable) return 0;
    g.createtable(state, 0, 0);
    while (count < limit && header->cmdTail != header->cmdHead) {
        char *slot = (char *)g.bus + CMD_BASE + (header->cmdTail % CMD_SLOTS) * CMD_SIZE;
        unsigned int op = *(unsigned int *)(slot + 0);
        unsigned int id = *(unsigned int *)(slot + 4);
        unsigned int length = *(unsigned int *)(slot + 8);
        if (length > CMD_SIZE - 20) length = CMD_SIZE - 20;
        g.createtable(state, 0, 3);
        g.pushnumber(state, (double)op);
        g.setfield(state, -2, "op");
        g.pushnumber(state, (double)id);
        g.setfield(state, -2, "id");
        g.pushls(state, slot + 12, length);
        g.setfield(state, -2, "payload");
        g.rawseti(state, -2, (int)count + 1);
        header->cmdTail = header->cmdTail + 1;
        count++;
    }
    return 1;
}

static int cb_bus_stop(lua_State *state)
{
    bus_header *header = (bus_header *)g.bus;
    (void)state;
    if (header) header->flags = header->flags | 1;
    return 0;
}

/* ------------------------------------------------------------------ registration */
static void field(lua_State *state, const char *name, void *function)
{
    if (!g.pushcclosure || !g.setfield) return;
    g.pushcclosure(state, function, 0, 0, 0);
    g.setfield(state, -2, name);
}

static void push_bus(lua_State *state)
{
    g.createtable(state, 0, 3);
    field(state, "push", (void *)cb_bus_push);
    field(state, "take", (void *)cb_bus_take);
    field(state, "stop", (void *)cb_bus_stop);
}

static void push_bridge(lua_State *state, const char *apiSource)
{
    const char *version = "0.1.0";
    g.createtable(state, 0, 24);
    field(state, "writefile", (void *)cb_writefile);
    field(state, "appendfile", (void *)cb_appendfile);
    field(state, "readfile", (void *)cb_readfile);
    field(state, "isfile", (void *)cb_isfile);
    field(state, "isfolder", (void *)cb_isfolder);
    field(state, "delfile", (void *)cb_delfile);
    field(state, "makefolder", (void *)cb_makefolder);
    field(state, "listfiles", (void *)cb_listfiles);
    field(state, "request", (void *)cb_request);
    field(state, "mousemoverel", (void *)cb_mousemoverel);
    field(state, "keypress", (void *)cb_keypress);
    field(state, "keyrelease", (void *)cb_keyrelease);
    field(state, "setclipboard", (void *)cb_setclipboard);
    field(state, "windowActive", (void *)cb_windowactive);
    field(state, "loadstring", (void *)cb_loadstring);
    field(state, "pump", (void *)cb_pump);
    if (apiSource && g.pushls && g.setfield) {
        g.pushls(state, apiSource, lstrlenA(apiSource));
        g.setfield(state, -2, "apiSource");
    }
    g.pushls(state, version, lstrlenA(version));
    g.setfield(state, -2, "version");
}


/* ------------------------------------------------------------------ resolve
 * Module bases come from the PEB so nothing has to be exported. Function addresses are the
 * measured RVAs against that base, sanity-checked to be executable and inside the recorded
 * code section, so a client update fails at the scan stage instead of calling into the void.
 */
/* LDR_DATA_TABLE_ENTRY, walked through InMemoryOrderModuleList (offset 0x10), so the three
   list links come first and the name is the second UNICODE_STRING at 0x58. */
typedef struct {
    LIST_ENTRY order;
    LIST_ENTRY memory;
    LIST_ENTRY init;
    unsigned long long base;          /* 0x30 */
    unsigned long long entry;         /* 0x38 */
    unsigned long size;               /* 0x40 */
    unsigned long reserved;
    unsigned short fullLength;        /* 0x48 */
    unsigned short fullMax;
    wchar_t *full;
    unsigned short nameLength;        /* 0x58 */
    unsigned short nameMax;
    wchar_t *name;                    /* 0x60 */
} module_entry;

static int same_name(const wchar_t *a, const wchar_t *b)
{
    while (*a && *b) {
        wchar_t x = *a, y = *b;
        if (x >= 'A' && x <= 'Z') x = (wchar_t)(x + 32);
        if (y >= 'A' && y <= 'Z') y = (wchar_t)(y + 32);
        if (x != y) return 0;
        a++;
        b++;
    }
    return *a == *b;
}

static unsigned char *module_base(const wchar_t *wanted)
{
    unsigned char *peb;
    HMODULE direct = GetModuleHandleW(wanted);
    if (direct) return (unsigned char *)direct;
    LIST_ENTRY *head, *cursor;
    LYPHEN_TRY {
        peb = (unsigned char *)__readgsqword(0x60);
        head = (LIST_ENTRY *)(*(unsigned long long *)(peb + 0x18) + 0x10);
        cursor = head->Flink;
        while (cursor && cursor != head) {
            module_entry *entry = (module_entry *)cursor;
            wchar_t name[64];
            unsigned short i;
            for (i = 0; i + 1 < entry->nameLength / 2 && i < 63; i++) name[i] = entry->name[i];
            name[i] = 0;
            if (same_name(name, wanted)) return (unsigned char *)entry->base;
            cursor = cursor->Flink;
        }
    }
    LYPHEN_EXCEPT(EXCEPTION_EXECUTE_HANDLER) {
        return 0;
    }
    return 0;
}

static int executable(void *address)
{
    MEMORY_BASIC_INFORMATION region;
    if (!VirtualQuery(address, &region, sizeof(region))) return 0;
    if (region.State != MEM_COMMIT) return 0;
    if (region.Protect == PAGE_EXECUTE_READ || region.Protect == PAGE_EXECUTE_READWRITE ||
        region.Protect == PAGE_EXECUTE_WRITECOPY || region.Protect == PAGE_EXECUTE)
        return 1;
    /* Hyperion parks most of .text at PAGE_NOACCESS with a guard and decrypts the page when the
       guard traps, so a committed page that is not marked executable yet is still a legitimate
       target - and it must stay guarded until something reads it (see safe_read). */
    return 1;
}

static void *at(unsigned char *base, unsigned int rva, const unsigned char *sig,
                size_t sigLength, const char *name)
{
    void *address;
    char line[128];
    char *o = line;
    const char *a = "rva ";
    if (!base) return 0;
    if (!rva) {
        /* unmeasured: scan the recorded prologue instead of giving up */
        if (!sig || !sigLength) {
            a = "unmeasured ";
            while (*a) *o++ = *a++;
            while (*name) *o++ = *name++;
            *o = 0;
            fail("scan", line);
            return 0;
        }
        address = scan_module((HMODULE)base, sig, sigLength);
        if (!address) {
            fail("scan", "prologue not found (client updated?)");
            return 0;
        }
        if (!executable(address)) return 0;
        return address;
    }
    if (rva > LYPHEN_TEXT_SIZE) {
        fail("scan", "rva outside the code section");
        return 0;
    }
    address = base + rva;
    if (!executable(address)) {
        fail("scan", "rva is not executable (client updated?)");
        return 0;
    }
    return address;
}

#define RESOLVE(field, type, suffix, label) \
    g.field = (type)at(base, LYPHEN_RVA_##suffix, \
                      LYPHEN_SIGLEN_##suffix ? LYPHEN_SIG_##suffix : 0, \
                      LYPHEN_SIGLEN_##suffix, label)

/* Runs the caller's block inside SEH so a bad guess fails closed instead of killing the client.
   Dead weight without a registered unwind table, which is why the entry registers one first. */
#ifdef _MSC_VER
#define GUARD(block) __try { block } __except (EXCEPTION_EXECUTE_HANDLER) { return 0; }
#else
#define GUARD(block) block           /* mingw has no SEH keyword; reads are validated instead */
#endif

/* an address we may safely deref for a read: committed, readable, executable, and inside the
   module with headroom for a prologue */
static int readable_code(unsigned char *base, void *address, size_t need)
{
    MEMORY_BASIC_INFORMATION region;
    unsigned char *at_ = (unsigned char *)address;
    if (at_ < base) return 0;
    if (!VirtualQuery(address, &region, sizeof(region))) return 0;
    if (region.State != MEM_COMMIT || (region.Protect & PAGE_NOACCESS)) return 0;
    if (!(region.Protect & (PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY |
                            PAGE_EXECUTE))) return 0;
    return (size_t)((unsigned char *)region.BaseAddress + region.RegionSize - at_) >= need;
}

/* Hyperion parks .text at PAGE_NOACCESS with guard pages and only backs/decrypts a page when a
   client thread takes the trap for it. Two consequences for us:
     - a page that was never executed reads back as nothing, so a byte scan can only ever see the
       pages the client already ran;
     - a guard page traps even when the bytes are there, and a frame handler sits *nearer* than
       the vectored handler, so catching it ourselves stops the decrypt.
   `arm_page` therefore clears the guard with VirtualProtectEx (no trap needed) and, for a page
   that is still no-access, lets one bare read take the fault and reach Hyperion. */
static int armed_page(const void *at, size_t size)
{
    MEMORY_BASIC_INFORMATION region;
    unsigned char *page = (unsigned char *)((ULONG_PTR)at & ~(ULONG_PTR)0xFFF);
    DWORD old = 0;
    if (!VirtualQuery(page, &region, sizeof(region))) return 0;
    if (region.State != MEM_COMMIT) return 0;
    if (region.Protect & PAGE_GUARD)
        VirtualProtectEx(GetCurrentProcess(), page, 0x1000, region.Protect & 0xFFu, &old);
    if (region.Protect == PAGE_NOACCESS) VirtualProtectEx(GetCurrentProcess(), page, 0x1000, 0x40, &old);
    (void)size;
    return 1;
}

#ifdef _MSC_VER
/* one bare read: the fault has to reach the vectored handler to get the page decrypted */
__declspec(noinline) static int decrypt_touch(const void *at)
{
    volatile unsigned char sink;
    __try {
        sink = *(const volatile unsigned char *)at;
    }
    __except (GetExceptionCode() == 0x80000001u ? EXCEPTION_CONTINUE_SEARCH
                                               : EXCEPTION_EXECUTE_HANDLER) {
        return 0;
    }
    (void)sink;
    return 1;
}
#else
static int decrypt_touch(const void *at) { return *(const volatile unsigned char *)at, 1; }
#endif

/* ---- text readability probe -------------------------------------------------------------
   Hyperion hands ReadProcessMemory zeros or fails outright for most of .text, so the only
   question that decides whether a byte scan can work at all is what *we* read from inside.
   Logged once per attach: rva, first bytes, and whether safe_read and a plain copy agree. */
static void probe_text(unsigned char *base)
{
    static const unsigned int rvas[] = { 0x1000u, 0x58c2a40u, 0x58c6820u, 0x58c8030u, 0x41dc170u,
                                         0x2584420u, 0x254da20u, 0x2566f90u, 0x58c8000u };
    char line[160];
    for (unsigned i = 0; i < sizeof(rvas) / sizeof(rvas[0]); i++) {
        unsigned char *at = base + rvas[i];
        const char *hexv = "0123456789abcdef";
        char *o = line;
        unsigned char direct[16] = { 0 };
        unsigned char viasafe[16] = { 0 };
        int touched;
        const char *p = "probe rva ";
        while (*p) *o++ = *p++;
        for (int shift = 28; shift >= 0; shift -= 4) *o++ = hexv[(rvas[i] >> shift) & 15];
        *o++ = ' ';
        {
            MEMORY_BASIC_INFORMATION where;
            int usable = VirtualQuery(at, &where, sizeof(where)) &&
                         where.State == MEM_COMMIT;
            touched = usable ? decrypt_touch(at) : -1;
        }
        { /* plain copy, the way a scanner would read it */
#ifdef _MSC_VER
            __try { for (int k = 0; k < 16; k++) direct[k] = at[k]; }
            __except (EXCEPTION_EXECUTE_HANDLER) { direct[0] = 0xEE; }
#else
            for (int k = 0; k < 16; k++) direct[k] = at[k];
#endif
        }
        for (int k = 0; k < 16; k++) { *o++ = hexv[direct[k] >> 4]; *o++ = hexv[direct[k] & 15]; }
        *o++ = ' ';
        if (safe_read(at, viasafe, 16)) {
            /* second copy: is the page stable across two reads? */
            unsigned char again[16] = { 0 };
            int same = safe_read(at, again, 16) && !lstrcmpA((char *)viasafe, (char *)again);
            for (int k = 0; k < 16; k++) { *o++ = hexv[again[k] >> 4]; *o++ = hexv[again[k] & 15]; }
            *o++ = ' ';
            *o++ = same ? 'S' : 'V';
            *o++ = touched ? 'T' : 't';
        } else {
            const char *f = "fail";
            while (*f) *o++ = *f++;
        }
        *o = 0;
        put(line);
    }
    put("probe done");
}

/* ---- pinned anchors ---------------------------------------------------------------------------
   patterns.h is full of zeros because the shipped text is packed and no rva could be read from
   the file. A measured set therefore comes from outside: the app drops the running client's
   FileVersion in build.txt and <LOCALAPPDATA>\Lyphen\anchors.txt carries per-version blocks.
   Complete match wins outright; anything less falls through to the in-memory calibration. */
typedef struct { const char *name; void **slot; } anchor_slot;

static char *read_chunk(const char *name);

static int pinned(unsigned char *base)
{
    static const anchor_slot slots[] = {
        { "load", (void **)&g.load }, { "call", (void **)&g.pcall },
        { "pushcclosure", (void **)&g.pushcclosure }, { "createtable", (void **)&g.createtable },
        { "setfield", (void **)&g.setfield }, { "rawseti", (void **)&g.rawseti },
        { "pushlstring", (void **)&g.pushls }, { "pushnumber", (void **)&g.pushnumber },
        { "pushboolean", (void **)&g.pushboolean }, { "tolstring", (void **)&g.tolstring },
        { "tonumber", (void **)&g.tonumber }, { "gettop", (void **)&g.gettop },
    };
    char *version, *text, *line;
    int have = 0, in_block = 0;
    size_t i;

    version = read_chunk("build.txt");
    if (!version) return 0;
    for (char *c = version; *c; c++) if (*c == '\r' || *c == '\n' || *c == ' ') { *c = 0; break; }
    if (!*version) return 0;

    text = read_chunk("anchors.txt");
    if (!text) return 0;

    line = text;
    while (*line) {
        char *end = line;
        while (*end && *end != '\n') end++;
        if (*end == '\n') *end++ = 0;

        if (line[0] == '[') {
            char *close = line + 1;
            while (*close && *close != ']') close++;
            *close = 0;
            in_block = !lstrcmpA(line + 1, version);
            line = end;
            continue;
        }
        if (in_block) {
            char *equals = line;
            while (*equals && *equals != '=') equals++;
            if (*equals == '=') {
                unsigned long long rva = 0;
                char *hex = equals + 1;
                while (*hex == ' ' || *hex == '\t') hex++;
                if (hex[0] == '0' && (hex[1] == 'x' || hex[1] == 'X')) hex += 2;
                for (; *hex; hex++) {
                    int digit = -1;
                    if (*hex >= '0' && *hex <= '9') digit = *hex - '0';
                    else if (*hex >= 'a' && *hex <= 'f') digit = *hex - 'a' + 10;
                    else if (*hex >= 'A' && *hex <= 'F') digit = *hex - 'A' + 10;
                    if (digit < 0) break;
                    rva = (rva << 4) | (unsigned)digit;
                }
                *equals = 0;
                for (i = 0; i < sizeof(slots) / sizeof(slots[0]); i++) {
                    if (lstrcmpA(line, slots[i].name)) continue;
                    if (!readable_code(base, base + rva, 16)) break;
                    *slots[i].slot = base + rva;
                    have++;
                    break;
                }
            }
        }
        line = end;
    }
    put(have >= 12 ? "pinned set" : "pinned set incomplete");
    return have >= 12;
}

static int resolve(void)
{
    unsigned char *base = module_base(L"RobloxPlayerBeta.exe");
    int ok = 1;
    if (!base) {
        fail("scan", "RobloxPlayerBeta.exe is not loaded");
        return 0;
    }
    GUARD(if (pinned(base)) { g.compile = 0; return 1; })
    put("r0 base ok");
    {
        char line[128];
        void *r2 = VirtualAlloc(0, 0x1000, MEM_RESERVE, PAGE_NOACCESS);
        SetLastError(0);
        void *c2 = r2 ? VirtualAlloc(r2, 0x1000, MEM_COMMIT, PAGE_READWRITE) : 0;
        DWORD e3 = GetLastError();
        { DWORD oldp = 0;
          if (c2 && !VirtualProtect(c2, 0x1000, PAGE_EXECUTE_READWRITE, &oldp)) c2 = 0; }
        {
            typedef long (NTAPI *nalloc_t)(void *, void **, ULONG_PTR, ULONG_PTR *, ULONG, ULONG);
            nalloc_t nalloc = (nalloc_t)GetProcAddress(GetModuleHandleA("ntdll.dll"), "NtAllocateVirtualMemory");
            void *n3 = 0; ULONG_PTR nsize = 0x1000;
            long st2 = nalloc ? nalloc(GetCurrentProcess(), &n3, 0, &nsize,
                                       MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE) : -1;
            char line2[96];
            wsprintfA(line2, "vprobe2 commit%p e%lu nt%ld %p", c2, e3, st2, n3);
            put(line2);
            if (n3) { ULONG_PTR fsize = 0; nalloc(GetCurrentProcess(), &n3, 0, &fsize, 0x8000, PAGE_NOACCESS); }
        }
        DWORD e2 = GetLastError();
        wsprintfA(line, "vprobe res%p commit%p e%lu", r2, c2, e2);
        put(line);
        if (r2) VirtualFree(r2, 0, MEM_RELEASE);
    }
    GUARD(probe_text(base);)
    RESOLVE(load, luau_load_t, LUAU_LOAD, "luau_load");
    put("r1 load");
    RESOLVE(pcall, lua_pcall_t, LUA_PCALL, "lua_pcall");
    put("r2 pcall");
    RESOLVE(pushcclosure, lua_pushcclosure_t, LUA_PUSHCCLOSURE, "lua_pushcclosure");
    put("r3 pushcclosure");
    RESOLVE(createtable, lua_createtable_t, LUA_CREATETABLE, "lua_createtable");
    put("r4 rest done");
    RESOLVE(setfield, lua_setfield_t, LUA_SETFIELD, "lua_setfield");
    RESOLVE(rawseti, lua_rawseti_t, LUA_RAWSETI, "lua_rawseti");
    RESOLVE(pushls, lua_pushlstring_t, LUA_PUSHLSTRING, "lua_pushlstring");
    RESOLVE(pushnumber, lua_pushnumber_t, LUA_PUSHNUMBER, "lua_pushnumber");
    RESOLVE(pushboolean, lua_pushboolean_t, LUA_PUSHBOOLEAN, "lua_pushboolean");
    RESOLVE(tolstring, lua_tolstring_t, LUA_TOLSTRING, "lua_tolstring");
    RESOLVE(tonumber, lua_tonumber_t, LUA_TONUMBER, "lua_tonumber");
    RESOLVE(gettop, lua_gettop_t, LUA_GETTOP, "lua_gettop");
    RESOLVE(loadstr, luaB_loadstring_t, LUAB_LOADSTRING, "luaB_loadstring");
    RESOLVE(pushvf, lua_pushvfstring_t, LUA_PUSHVFSTRING, "lua_pushvfstring");
    /* these two are lua_CFunction shaped; the cast keeps the macro single-shaped */
    RESOLVE(luaBpcall, lua_cfunction_t, LUAB_PCALL, "luaB_pcall");
    RESOLVE(luaBprint, lua_cfunction_t, LUAB_PRINT, "luaB_print");

    /* nothing measured yet (the shipped text is packed, so these are normally all zero):
       calibrate in memory and keep whatever worked */
    if ((!g.load && !(g.loadstr && g.pushvf)) || !g.pcall) {
        put("r5 calibrate");
        if (!calibrate(base)) emit("e", 0, "calibration failed, the executor stays dumb");
        put("r6 calibrated");
        g.compile = 0;   /* discovered lazily below */
    }
    ok = (g.load || (g.loadstr && g.pushvf)) && g.pcall &&
         g.pushcclosure && g.createtable && g.setfield && g.rawseti;
    return ok;
}

/* the runner ships as a file next to the app so a Luau change needs no rebuild */
static char *read_chunk(const char *name)
{
    char path[MAX_PATH];
    HANDLE file;
    DWORD size = 0, read = 0;
    char *buffer;
    GetEnvironmentVariableA("LOCALAPPDATA", path, MAX_PATH);
    lstrcatA(path, "\\Lyphen\\");
    lstrcatA(path, name);
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


/* ------------------------------------------------------------------ self-calibration
 * The shipped .text is packed (Byfron), so nothing can be read off the file: every prologue
 * signature only exists once the client has unpacked itself. Strings in .rdata are not packed
 * though, so we find a known name, then find the code that references it in memory, then take
 * the call targets inside that function. Whatever we settle on is reported so a live run can
 * be turned into fixed RVAs afterwards.
 */
static int in_range(void *at, unsigned char *lo, unsigned char *hi)
{
    return (unsigned char *)at >= lo && (unsigned char *)at < hi;
}

/* is the address inside any committed executable region? */
/* how far a scan may walk inside this module: mapped size minus the header page */
static size_t code_span(unsigned char *base)
{
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt;
    size_t span;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return 0;
    span = nt->OptionalHeader.SizeOfImage;
    if (span <= 0x10000) return 0;
    return span - 0x10000;
}

static void *exec_target(unsigned long long address)
{
    MEMORY_BASIC_INFORMATION region;
    void *at = (void *)address;
    if (!VirtualQuery(at, &region, sizeof(region))) return 0;
    if (region.State != MEM_COMMIT) return 0;
    if (region.Protect != PAGE_EXECUTE_READ && region.Protect != PAGE_EXECUTE_READWRITE &&
        region.Protect != PAGE_EXECUTE_WRITECOPY && region.Protect != PAGE_EXECUTE)
        return 0;
    return at;
}

/* printable, NUL-terminated, plain ASCII: .rdata strings are readable even packed */
static int is_name(void *where, size_t min, size_t max)
{
    char *at = (char *)where;
    char buffer[64];
    size_t i;
    if (!safe_read(at, buffer, min)) return 0;
    for (i = 0; i < min - 1; i++)
        if (buffer[i] < 0x20 || buffer[i] > 0x7E) return 0;
    for (; i < max - 1; i++) {
        if (!safe_read(at + i, buffer + i, 1)) return 0;
        if (buffer[i] == 0) return 1;
        if (buffer[i] < 0x20 || buffer[i] > 0x7E) return 0;
    }
    return 0;
}

/* find "name" in the module's read-only data */
static char *find_name(unsigned char *base, const char *name, size_t size, char *from)
{
    /* one page is copied once and then searched in memory: a safe_read per byte costs minutes on
       a 50 MB data section, and the hijacked client thread has to be given back quickly */
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt;
    IMAGE_SECTION_HEADER *section;
    unsigned char page_buf[0x1000 + 64];
    int i;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    section = IMAGE_FIRST_SECTION(nt);
    for (i = 0; i < nt->FileHeader.NumberOfSections; i++, section++) {
        unsigned char *page, *stop;
        if (!(section->Characteristics & IMAGE_SCN_MEM_READ)) continue;
        if (section->Characteristics & IMAGE_SCN_MEM_EXECUTE) continue;   /* packed */
        page = base + section->VirtualAddress;
        stop = page + section->Misc.VirtualSize;
        for (; page < stop; page += 0x1000) {
            size_t len = (size_t)(stop - page);
            unsigned char *at;
            size_t k;
            if (len > 0x1000) len = 0x1000;
            if (!safe_read(page, page_buf, len + 48)) continue;
            for (k = 0; k + size + 1 < len + 48; k++) {
                at = page + k;
                if (from && at < (unsigned char *)from) continue;
                if (memcmp(page_buf + k, name, size)) continue;
                if (page_buf[k + size] != 0) continue;
                if (is_name(at, size + 1, 32)) return (char *)at;
            }
        }
    }
    return 0;
}

/* rip-relative references to a fixed address, in executable memory only */
static void *find_reference(void *needle, size_t limit)
{
    /* the referencing code is always inside the client image; each page is copied once and then
       scanned in memory */
    unsigned char *base = module_base(L"RobloxPlayerBeta.exe");
    unsigned char buffer[0x1000 + 64];
    size_t span;
    unsigned char *page, *end;
    size_t seen = 0;
    (void)limit;
    if (!base) return 0;
    span = code_span(base);
#ifdef LYPHEN_HOST
    { extern int printf(const char *fmt, ...); printf("  [dbg] find_reference span=%llx needle=%p\n",
        (unsigned long long)span, needle); }
#endif
    if (!span) return 0;
    page = 0;
    while ((page = next_scan(base, span, page, &end)) != 0) {
        size_t len = (size_t)(end - page);
        size_t k;
        if (len > 0x1000) len = 0x1000;
        if (!safe_read(page, buffer, len)) { page = step_page(page, end);
#ifdef LYPHEN_HOST
            { extern int printf(const char *fmt, ...); extern char *getenv(const char *);
              if (getenv("LYPHEN_DEEP2")) printf("  [dbg] unreadable page %p len %llx\n", (void *)page, (unsigned long long)len); }
#endif
            continue; }
#ifdef LYPHEN_HOST
        { extern int printf(const char *fmt, ...); extern char *getenv(const char *);
          if (getenv("LYPHEN_DEEP2")) printf("  [dbg] page %p len %llx\n", (void *)page, (unsigned long long)len); }
#endif
        for (k = 0; k + 8 <= len; k++, seen++) {
            unsigned char *head = buffer + k;
            long displacement;
            unsigned char *target;
            if (seen > (192u << 20)) return 0;
            if ((head[0] & 0xF8) != 0x48) continue;              /* REX.W */
            /* lea r64,[rip+d] takes the address, mov r64,[rip+d] loads a slot holding it */
            if (head[1] != 0x8D && head[1] != 0x8B) continue;
            if ((head[2] & 0xC7) != 0x05) continue;              /* [rip + disp32] */
            displacement = *(long *)&head[3];
            target = page + k + 7 + displacement;
#ifdef LYPHEN_HOST
            { extern int printf(const char *fmt, ...); extern char *getenv(const char *);
              static int shown; if (getenv("LYPHEN_DEEP") && shown < 6000)
                  printf("  [dbg] ref-cand at %p -> %p (want %p)\n", (void *)(page + k),
                         (void *)target, needle), shown++; }
#endif
            if (target == (unsigned char *)needle) {
#ifdef LYPHEN_HOST
                { extern int printf(const char *fmt, ...);
                  printf("  [dbg] ref MATCH at %p for %p\n", (void *)(page + k), needle); }
#endif
                return page + k;
            }
            if (head[1] == 0x8B) {
                void *slot = 0;
                if (safe_read(target, &slot, sizeof(slot)) && slot == needle) return page + k;
            }
        }
        page = step_page(page, end);
    }
#ifdef LYPHEN_HOST
    { extern int printf(const char *fmt, ...); extern char *getenv(const char *);
      if (getenv("LYPHEN_DEEP")) printf("  [dbg] ref none for %p\n", needle); }
#endif
    return 0;
}

/* call targets (E8 rel32) inside a window of a function */
static int gather_calls(unsigned char *at, size_t window, void **out, int max)
{
    int found = 0;
    size_t i;
    for (i = 0; i < window && found < max; i++) {
        unsigned char head[5];
        long displacement;
        if (!safe_read(at + i, head, sizeof(head))) return found;
        if (head[0] != 0xE8) continue;
        displacement = *(long *)&head[1];
        if (exec_target((unsigned long long)(at + i + 5 + displacement)))
            out[found++] = at + i + 5 + displacement;
    }
    return found;
}

/* luau_load and lua_pcall both store L->top: `mov [reg+0x80], reg` with a disp32. Requiring
   the exact displacement and a nearby ret keeps ordinary prologues out of the shortlist. */
static int writes_top(void *function)
{
    unsigned char code[112];
    int i, ret = 0;
    if (!safe_read(function, code, sizeof(code))) return 0;
    for (i = 0; i < 100; i++) {
        if (code[i] == 0xC3) { ret = 1; break; }
        if (code[i] != 0x48 && code[i] != 0x4C && code[i] != 0x49) continue;
        if (code[i + 1] != 0x89) continue;
        /* modrm with mod=10 (disp32) and rm != 100/101 */
        if ((code[i + 2] & 0xC0) != 0x80) continue;
        if (*(int *)&code[i + 3] == 0x80) return 1;
    }
    return ret ? 0 : 0;
}

/* One anchor per entry point, because neither function is reachable from the other:
     luau_load : luaB_loadstring compiles first and loads last, so take the *last* top-writing
                 callee in it (an earlier one is the compiler).
     lua_pcall : luaB_pcall calls it directly, and that is the whole body.
   Both are guesses, so the pick is reported and can be frozen into patterns.json later. */
static void *anchor_calls(unsigned char *base, const char *name, size_t nameLength, int pickLast)
{
    char *hit = 0;
    void *chosen = 0;
    if (!base) return 0;
    hit = find_name(base, name, nameLength, 0);
#ifdef LYPHEN_HOST
    { extern int printf(const char *fmt, ...); printf("  [dbg] find_name %s -> %p\n", name, (void *)hit); }
#endif
    while (hit && !chosen) {
        void *ref = find_reference(hit, 192u << 20);
        int found = 0;
        if (ref) {
            void *calls[24];
            int i;
            found = gather_calls((unsigned char *)ref, 320, calls, 24);
#ifdef LYPHEN_HOST
            { extern int printf(const char *fmt, ...); extern char *getenv(const char *);
              if (getenv("LYPHEN_STAGES"))
                  printf("  [dbg] anchor %s hit=%p ref=%p calls=%d pickLast=%d\n", name,
                         (void *)hit, (void *)ref, found, pickLast); }
#endif
            for (i = 0; i < found; i++) {
                int at = pickLast ? found - 1 - i : i;
                if (writes_top(calls[at])) { chosen = calls[at]; break; }
            }
        }
        hit = find_name(base, name, nameLength, hit + 1);
    }
    return chosen;
}

/* the nearest call target before `at`, for entries that always precede another */
static void *previous_call(unsigned char *at, size_t window)
{
    size_t i;
    for (i = 1; i < window; i++) {
        unsigned char head[5];
        long displacement;
        void *target;
        if (!safe_read(at - i, head, sizeof(head))) return 0;
        if (head[0] != 0xE8) continue;
        displacement = *(long *)&head[1];
        target = at - i + 5 + displacement;
        if (exec_target((unsigned long long)target) && writes_top(target)) return target;
    }
    return 0;
}

/* lua_gettop is (top - base) / 16 on the state header and nothing else: a very distinct shape */
/* The client's text is packed: only pages the game has already run are decrypted, the rest read
   as zeros (or are not committed at all). Every scan therefore walks readable, non-zero pages
   instead of the flat range, and reports the page end so it never reads past it. */
static unsigned char *next_scan(unsigned char *base, size_t span, unsigned char *from,
                               unsigned char **end)
{
    MEMORY_BASIC_INFORMATION region;
    unsigned char *limit = base + span;
    if (from < base + 0x1000) from = base + 0x1000;
    while (from < limit) {
        unsigned char probe[4];
        if (!VirtualQuery(from, &region, sizeof(region))) return 0;
        *end = (unsigned char *)region.BaseAddress + region.RegionSize;
        if (*end > limit) *end = limit;
        from = (unsigned char *)region.BaseAddress;
        if (*end <= from) *end = from + 0x1000;      /* never hand back a zero-length region */
        if (*end > limit) *end = limit;
        if (region.State == MEM_COMMIT && (region.Protect & 0xFF) != PAGE_NOACCESS && *end - from >= 64 &&
            safe_read(from, probe, 4) && (probe[0] | probe[1] | probe[2] | probe[3]))
            return from;
        from = *end;
        if (from <= base) from = base + 0x1000;      /* belt and braces against no progress */
    }
    return 0;
}

static void *find_gettop(unsigned char *base)
{
    size_t span = code_span(base);
    unsigned char *page, *end, *at;
    if (!span) return 0;
    page = 0;
    while ((page = next_scan(base, span, page, &end)) != 0) {
        for (at = page; at + 32 <= end; at++) {
            unsigned char c[13];
            int reg;
            if (!safe_read(at, c, 13)) break;
            /* mov rX,[rcx+0x80] / sub rX,[rcx+0x78] / sar rX,4 / ret, all on one register */
            if (c[0] != 0x48 || c[1] != 0x8B || c[3] != 0x80) continue;
            reg = c[2];
            if (c[4] != 0x48 || c[5] != 0x2B || c[6] != reg || c[7] != 0x78) continue;
            if (c[8] != 0x48 || c[9] != 0xC1 || (c[10] & 0xF8) != 0xF8 || (c[10] & 7) != (reg & 7)) continue;
            if (c[11] != 0x04 || c[12] != 0xC3) continue;
            return at;
        }
        page = step_page(page, end);
    }
    return 0;
}

/* Every lua_push* ends by advancing the stack pointer: add qword [rcx+0x80], 0x10, then ret.
   That is common to all of them, so they are told apart by what they store first. */
static void *find_push(int kind)
{
    unsigned char *base = module_base(L"RobloxPlayerBeta.exe");
    size_t i, span = code_span(base);
    unsigned char *start;
    if (!base || !span) return 0;
    start = base + 0x1000;
    for (i = 0; i + 40 < span; i++) {
        unsigned char code[40];
        int found = -1, j;
        if (!safe_read(start + i, code, sizeof(code))) return 0;
        for (j = 0; j < 30; j++)
            if (code[j] == 0x48 && code[j + 1] == 0x83 && code[j + 2] == 0x41 &&
                code[j + 3] == 0x80 && code[j + 4] == 0x10) { found = j; break; }
        if (found < 0) continue;
        /* a push ends right after the advance */
        if (code[found + 5] != 0xC3 && !(code[found + 5] >= 0xC0 && code[found + 5] <= 0xCF)) continue;
        if (kind == 1) {                       /* boolean: a byte store from dl/edx */
            for (j = 1; j < found; j++)
                if (code[j] == 0x88 && (code[j + 1] & 0xC7) == 0x01) return start + i;
        } else if (kind == 2) {                /* number: an SSE store */
            for (j = 1; j < found; j++)
                if (code[j] == 0xF2 && code[j + 1] == 0x0F && code[j + 2] == 0x11) return start + i;
        } else {                               /* string/table: an internal call */
            for (j = 2; j < found; j++)
                if (code[j] == 0xE8) return start + i;
        }
    }
    return 0;
}

static const unsigned char pop_one[] = { 0x48, 0x83, 0x69, 0x80, 0x10 };
static const unsigned char pop_two[] = { 0x48, 0x83, 0x69, 0x80, 0x20 };

/* setters pop before they work: one slot for setfield, two for rawseti */
static void *find_pop(const unsigned char *needle)
{
    unsigned char *base = module_base(L"RobloxPlayerBeta.exe");
    size_t i, span = code_span(base);
    unsigned char *start;
    if (!base || !span) return 0;
    start = base + 0x1000;
    for (i = 0; i + 48 < span; i++) {
        unsigned char code[48];
        int j, call = 0;
        if (!safe_read(start + i, code, sizeof(code))) return 0;
        if (memcmp(code, needle, 5)) continue;
        for (j = 5; j < 44; j++) if (code[j] == 0xE8) { call = 1; break; }
        if (call) return start + i;
    }
    return 0;
}

/* a table constructor: allocates through a call, then pushes. It takes three arguments, so the
   third one has to be read somewhere in the first bytes - that is what separates it from the
   string pusher, which only takes one. */
static void *find_createtable(void)
{
    unsigned char *base = module_base(L"RobloxPlayerBeta.exe");
    size_t i, span = code_span(base);
    unsigned char *start;
    if (!base || !span) return 0;
    start = base + 0x1000;
    for (i = 0; i + 56 < span; i++) {
        unsigned char code[56];
        int j, call = -1, third = 0;
        if (!safe_read(start + i, code, sizeof(code))) return 0;
        for (j = 0; j + 5 < 40; j++)
            if (code[j] == 0x48 && code[j + 1] == 0x83 && code[j + 2] == 0x41 &&
                code[j + 3] == 0x80 && code[j + 4] == 0x10) break;
        if (j + 5 >= 40) continue;
        for (int k = 1; k < j; k++) if (code[k] == 0xE8) { call = k; break; }
        if (call < 0) continue;
        for (int k = 0; k < call; k++) if (code[k] == 0x44 || code[k] == 0x45) { third = 1; break; }
        if (third) return start + i;
    }
    return 0;
}

/* Accessors that take an index work on `L->top - 0x10` (the -1 slot) and convert in place.
   tonumber also reports through its third argument, a store into [r8] - that is the only
   reliable difference between the two, so it decides which is which. */
static void *find_indexed(int wantNumber)
{
    unsigned char *base = module_base(L"RobloxPlayerBeta.exe");
    size_t i, span = 2u << 20;
    static const unsigned char pickLast[] = { 0x48, 0x8B, 0x41, 0x80, 0x48, 0x83, 0xE8, 0x10 };
    if (!base) return 0;
    for (i = 0x1000; i + 80 < span; i++) {
        unsigned char code[80];
        int j, call = 0, writesOut = 0;
        if (!safe_read(base + i, code, sizeof(code))) return 0;
        if (memcmp(code, pickLast, sizeof(pickLast))) continue;
        for (j = 8; j < 72; j++) {
            if (code[j] == 0xE8) call = 1;
            if (code[j] == 0x41 && (code[j + 1] == 0x88 || code[j + 1] == 0xC6 ||
                                    code[j + 1] == 0xC7 || code[j + 1] == 0xB0))
                writesOut = 1;
        }
        if (!call) continue;
        if (writesOut == wantNumber) return base + i;
    }
    return 0;
}

static void report_pick(const char *label, void *at)
{
    char line[96];
    wsprintfA(line, "calibrated %s=%p", label, at);
    emit("i", 0, line);
    put(line);
}

static int calibrate(unsigned char *base)
{
    if (!base) base = module_base(L"RobloxPlayerBeta.exe");
    if (!base) {
        fail("scan", "no client module to calibrate against");
        return 0;
    }
    g.load = (luau_load_t)anchor_calls(base, "loadstring", 10, 1);
    put("c1 load calls");
    g.pcall = (lua_pcall_t)anchor_calls(base, "pcall", 5, 0);
    put("c2 pcall calls");
    if (g.load) report_pick("load", (void *)g.load);
    else fail("scan", "no load anchor found");
    if (!g.compile) {
        /* the compile call sits immediately before the load call in luaB_loadstring */
        unsigned char *loadAt = (unsigned char *)g.load;
        if (loadAt) g.compile = (luau_compile_t)previous_call(loadAt, 0x400);
    put("c3 prev call");
    }

    if (!g.pcall) {
        /* the pcall entry often references it indirectly; fall back to the other top-writing
           callee in loadstring, which is the compile/push side */
        g.pcall = (lua_pcall_t)anchor_calls(base, "loadstring", 10, 0);
    put("c4 alt");
        if ((void *)g.pcall == (void *)g.load) g.pcall = 0;
    }
    if (g.pcall) report_pick("call", (void *)g.pcall);
    else fail("scan", "no call anchor found");
    if (!g.gettop && (g.gettop = (lua_gettop_t)find_gettop(base)) != 0)
    put("c5 gettop");
        report_pick("gettop", (void *)g.gettop);
    if (g.compile) report_pick("compile", (void *)g.compile);
    if (!g.pushboolean && (g.pushboolean = (lua_pushboolean_t)find_push(1)) != 0)
    put("c6 push");
        report_pick("pushbool", (void *)g.pushboolean);
    if (!g.pushnumber && (g.pushnumber = (lua_pushnumber_t)find_push(2)) != 0)
        report_pick("pushnum", (void *)g.pushnumber);
    if (!g.pushls && (g.pushls = (lua_pushlstring_t)find_push(3)) != 0)
        report_pick("pushstr", (void *)g.pushls);
    if (!g.setfield && (g.setfield = (lua_setfield_t)find_pop(pop_one)) != 0)
    put("c7 pop");
        report_pick("setfield", (void *)g.setfield);
    if (!g.rawseti && (g.rawseti = (lua_rawseti_t)find_pop(pop_two)) != 0)
        report_pick("rawseti", (void *)g.rawseti);
    if (!g.createtable && (g.createtable = (lua_createtable_t)find_createtable()) != 0)
        report_pick("newtable", (void *)g.createtable);
    if (!g.tonumber && (g.tonumber = (lua_tonumber_t)find_indexed(1)) != 0)
        report_pick("tonumber", (void *)g.tonumber);
    if (!g.tolstring && (g.tolstring = (lua_tolstring_t)find_indexed(0)) != 0)
        report_pick("tolstring", (void *)g.tolstring);
    return g.load && g.pcall && g.pcall != (lua_pcall_t)g.load;
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

/* pull "code":"..." out of a line, unescaping as we go */
static unsigned int json_string(const char *line, const char *key, char *out, unsigned int size)
{
    const char *at = find_text(line, key);
    unsigned int length = 0;
    if (!at) return 0;
    at += lstrlenA(key);
    while (*at && *at != '"' && length + 1 < size) out[length++] = *at++;
    out[length] = 0;
    return length;
}

static unsigned int json_number(const char *line, const char *key)
{
    const char *at = find_text(line, key);
    unsigned int value = 0;
    if (!at) return 0;
    at += lstrlenA(key);
    while (*at >= '0' && *at <= '9') value = value * 10 + (unsigned int)(*at++ - '0');
    return value;
}

/* queue one command for the runner; a full ring drops it and says so */
static void push_command(unsigned int op, unsigned int id, const char *payload)
{
    bus_header *h = (bus_header *)g.bus;
    char *slot;
    unsigned int length = payload ? (unsigned int)lstrlenA(payload) : 0;
    if (!h || h->cmdHead - h->cmdTail >= CMD_SLOTS) {
        emit("e", id, "command ring full");
        return;
    }
    if (length > CMD_SIZE - 20) length = CMD_SIZE - 20;
    slot = (char *)g.bus + CMD_BASE + (h->cmdHead % CMD_SLOTS) * CMD_SIZE;
    *(unsigned int *)(slot + 0) = op;
    *(unsigned int *)(slot + 4) = id;
    *(unsigned int *)(slot + 8) = length;
    for (unsigned int i = 0; i < length; i++) slot[12 + i] = payload[i];
    h->cmdHead = h->cmdHead + 1;
}

/* server side: the gui is the client and connects by pid. The connect borrows the hijacked
   thread for a moment at most; afterwards everything runs off the pipe and Heartbeat. */
static int open_pipe(void)
{
    char name[64];
    wsprintfA(name, "\\\\.\\pipe\\lyphen-%u", GetCurrentProcessId());
    g.pipe = CreateNamedPipeA(name, PIPE_ACCESS_DUPLEX,
                              PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT,
                              PIPE_UNLIMITED_INSTANCES, 8192, 8192, 0, 0);
    if (g.pipe == INVALID_HANDLE_VALUE) return 0;
    /* NOWAIT: 535 means a client is already there, 536 means we are listening - both fine,
       and either way the connection completes by itself when the gui connects */
    if (ConnectNamedPipe(g.pipe, 0) == 0 && GetLastError() != ERROR_PIPE_CONNECTED &&
        GetLastError() != ERROR_PIPE_LISTENING) {
        CloseHandle(g.pipe);
        g.pipe = INVALID_HANDLE_VALUE;
        return 0;
    }
    /* The gui cannot enumerate //./pipe/ while we hammer the pipe in a wait loop (it hangs), so
       the pipe is announced with a file instead: one name per pid, deleted when we go away. */
    {
        char path[MAX_PATH];
        HANDLE mark;
        workspace(path);
        lstrcatA(path, "\\");
        {
            char tail[32];
            wsprintfA(tail, "up-%u.tmp", GetCurrentProcessId());
            lstrcatA(path, tail);
        }
        mark = CreateFileA(path, GENERIC_WRITE, 0, 0, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, 0);
        if (mark != INVALID_HANDLE_VALUE) CloseHandle(mark);
    }
    return 1;
}

/* everything the runner and the core emit leaves through here, in one place: bus slots in,
   pipe lines out. Numbers the runner uses map onto the gui's ops. */
static void drain_output(void)
{
    bus_header *h = (bus_header *)g.bus;
    char line[700];
    while (h && h->outTail != h->outHead) {
        char *slot = (char *)g.bus + OUT_BASE + (h->outTail % OUT_SLOTS) * OUT_SIZE;
        unsigned int kind = *(unsigned int *)(slot + 0);
        unsigned int id = *(unsigned int *)(slot + 4);
        unsigned int length = *(unsigned int *)(slot + 8);
        const char *op = "log";
        const char *label = "info";
        char *o = line;
        const char *a;

        if (length > OUT_SIZE - 16) length = OUT_SIZE - 16;
        /* kind arrives as a character from emit() and as 1..5 from the runner */
        if (kind < 8) {
            if (kind == 1) label = "print";
            else if (kind == 2) label = "warn";
            else if (kind == 3) { label = "error"; op = "fail"; }
            else if (kind == 5) op = "done";
        } else if (kind == 'e') { label = "error"; op = "fail"; }
        else if (kind == 'w') { label = "warn"; }

        a = "{\"op\":\"";
        while (*a) *o++ = *a++;
        a = op;
        while (*a) *o++ = *a++;
        *o++ = '"';
        if (kind == 5) {
            a = ",\"id\":";
            while (*a) *o++ = *a++;
            char digits[12];
            wsprintfA(digits, "%u", id);
            a = digits;
            while (*a) *o++ = *a++;
            a = ",\"ms\":0}\n";
            while (*a) *o++ = *a++;
            *o = 0;
            pipe_line(line);
            h->outTail = h->outTail + 1;
            continue;
        }
        a = ",\"kind\":\"";
        while (*a) *o++ = *a++;
        a = label;
        while (*a) *o++ = *a++;
        a = "\",\"script\":";
        while (*a) *o++ = *a++;
        char digits[12];
        wsprintfA(digits, "%u", id);
        a = digits;
        while (*a) *o++ = *a++;
        a = ",\"text\":\"";
        while (*a) *o++ = *a++;
        for (unsigned int i = 0; i < length && o < line + 660; i++) {
            unsigned char c = (unsigned char)slot[12 + i];
            if (c == '"' || c == '\\') *o++ = '\\';
            if (c == '\n') { *o++ = '\\'; *o++ = 'n'; continue; }
            if (c == '\r') continue;
            *o++ = (char)c;
        }
        a = "\"}\n";
        while (*a) *o++ = *a++;
        *o = 0;
        pipe_line(line);
        h->outTail = h->outTail + 1;
    }
}

/* trailing text still waiting in the read buffer, so a fragment is not half-parsed */
static char g_carry[4096];
static volatile long g_patchAck;     /* rva the gui answered, +1 */
static volatile long g_patchOk;
static volatile long g_guiUp;        /* the gui has said hello: only then can it answer */
static unsigned int g_carryLength;

static void handle_line(const char *line)
{
    unsigned int id = json_number(line, "\"id\":");
    static char code[8192];
    unsigned int length;
    if (find_text(line, "\"exec\"")) {
        length = json_string(line, "\"code\":\"", code, sizeof(code));
        if (!length) { emit("e", id, "exec with no code"); return; }
        push_command(OP_EXEC, id ? id : ++g.nextId, code);
    } else if (find_text(line, "\"cancel\"")) {
        push_command(OP_CANCEL, id, "");
    } else if (find_text(line, "\"patched\"")) {
        long rva = (long)json_number(line, "\"rva\":");
        put("rp ack seen");
        g_patchOk = find_text(line, "\"ok\":true") ? 1 : 0;
        g_patchAck = rva + 1;
    } else if (find_text(line, "\"hello\"")) {
        g_guiUp = 1;
        char hello[160];
        wsprintfA(hello, "{\"op\":\"ready\",\"pid\":%u,\"build\":\"%s\"}\n",
                  GetCurrentProcessId(), LYPHEN_CLIENT_VERSION);
        pipe_line(hello);
    } else if (find_text(line, "\"bye\"")) {
        push_command(OP_SHUTDOWN, 0, "");
    }
}

/* One non-blocking pass: read what the gui sent, forward what the runner produced. Called from
   the runner's Heartbeat through the bridge, which is why the core needs no thread of its own. */
static void pump(void)
{
    /* GUI <-> core: one JSON object per line. Never blocks the client: the pipe is NOWAIT and
       an idle pass only sleeps. */
    char chunk[4096];
    DWORD read = 0;

    if (g.pipe != INVALID_HANDLE_VALUE) {
        if (ReadFile(g.pipe, chunk, sizeof(chunk) - 1, &read, 0) && read > 0) {
            chunk[read] = 0;
            for (DWORD i = 0; i < read; i++) {
                char c = chunk[i];
                if (c == '\n' || c == 0) {
                    g_carry[g_carryLength] = 0;
                    if (g_carryLength > 1) handle_line(g_carry);
                    g_carryLength = 0;
                } else if (g_carryLength + 1 < sizeof(g_carry)) {
                    g_carry[g_carryLength++] = c;
                }
            }
        } else if (GetLastError() == ERROR_BROKEN_PIPE) {
            DisconnectNamedPipe(g.pipe);
            CloseHandle(g.pipe);
            g.pipe = INVALID_HANDLE_VALUE;
        }
    }
    drain_output();
}

/* the bridge form: safe to call every frame, never blocks */
static int cb_pump(lua_State *state)
{
    (void)state;
    pump();
    return 0;
}


/* ------------------------------------------------------------------ VM thread handoff
 * Luau is not thread safe: touching a lua_State while the client's own thread is inside it
 * corrupts its stack and the client dies a frame or two later. So nothing is called from the
 * injected thread. A hook on lua_pcall (the client calls it constantly) gives us the VM thread,
 * in a state where a call is expected, and the runner is installed from there exactly once.
 */
static unsigned char *g_trampoline;  /* zero when the stubs live in g_stubs instead */
static unsigned char *g_replay;      /* replay buffer of the hook being installed */
static unsigned char *g_hookArena;   /* next free stub slot; each hook owns 256 bytes */
/* A 12-byte absolute jump reaches anywhere, so the stubs do not have to be allocated: they sit in
   our own image, which is committed before the client can exhaust its commit limit. */
static int g_stubsReady;
static DWORD wasArena;
#define DRY_PATCH 0
#define HOOK_SLOT 256
#define HOOK_SLOTS (4096 / HOOK_SLOT)
/* Arbitrary Code Guard is on in the client ("the operation was blocked as the process prohibits
   dynamic code generation"), so a private page can never be made executable. A page in our own
   image can be, and if the section arrives executable it needs no protection change at all. */
#ifdef _MSC_VER
#pragma section(".lystub", execute, read, write)
__declspec(allocate(".lystub")) static unsigned char g_stubs[HOOK_SLOTS * HOOK_SLOT];
#define STUB_ARENA_OK 1
#else
/* mingw ignores #pragma section and __declspec(allocate), so g_stubs would sit in .data and the
   first jump into it would fault on execute. A private page is right for the harness. */
static unsigned char g_stubs[HOOK_SLOTS * HOOK_SLOT];
#define STUB_ARENA_OK 0
#endif

/* one record per live hook: re-installing on the same target must rebuild from the *original*
   bytes, because at that point the first bytes are our own E9 and replaying those would jump to
   the old stub, whose tail then lands in the middle of the new patch. */
typedef struct {
    void *target;
    int displaced;
    unsigned char orig[32];
} HOOKREC;
static HOOKREC g_hooks[HOOK_SLOTS];
static int g_hookCount;
#ifdef LYPHEN_HOST
#define HOST_ONLY(x) (x)
static int g_noThunk = 1;   /* bisect: replay only, no thunk call */                   /* harness switches: shorten the stub, silence it */
static int g_quietThunk;
#else
#define HOST_ONLY(x) 0
enum { g_quietThunk = 1 };
#endif
static void *g_hookTarget;
static int g_installed;
static int g_started;                   /* the entry may be entered more than once */
int g_installed_via_thunk;              /* read by the harness: prove we landed on the VM thread */
static int g_hooked;
static char *g_apiSource;
static char *g_runnerSource;
static int g_inThunk;                   /* thunk depth: install once, never from inside itself */

static void install_now(void *hint);

/* called instead of the hooked function; its return goes through the replay stub */
static void hook_thunk(void *first)
{
#ifdef LYPHEN_HOST
    if (!g_quietThunk) printf("  [dbg] thunk\n");
#endif
    g_installed_via_thunk = 1;
    /* Installing runs Luau, and the runner's own calls re-enter this same hooked function while
       the first stub frame is still live: without the depth guard that recursion walks the stack
       away. Nested entries just replay the prologue and return. */
    if (g_inThunk) return;
    g_inThunk = 1;
    put("hk fire");
    if (!g_installed) install_now(first);
    g_inThunk = 0;
    put("hk done");
}

/* Length of one instruction, enough for the prologues these wrappers start with. Returns 0 on
   anything it does not understand, and the hook is then skipped rather than guessed at. */
static int g_insn_rip;                  /* set when the last decoded instruction was rip-relative */

static int sub_insn_len(const unsigned char *code)
{
    int i = 0;
    int rex = 0;
    unsigned char op;
    int modrm = 0, mod, rm, disp = 0, imm = 0;

    while (i < 4 && (code[i] == 0x66 || code[i] == 0x67 || code[i] == 0xF2 || code[i] == 0xF3))
        i++;
    if (i < 4 && code[i] >= 0x40 && code[i] <= 0x4F) { rex = 1; i++; }
    if (i >= 8) return 0;
    g_insn_rip = 0;
    op = code[i++];

    if (op >= 0x50 && op <= 0x5F) return i;                 /* push/pop reg */
    if (op == 0xC3 || op == 0xCC || op == 0x90 || op == 0xF8) return i;
    if (op >= 0xB8 && op <= 0xBF) return i + (rex ? 8 : 4); /* mov reg, imm */
    if (op == 0xE8 || op == 0xE9) return i + 4;
    if (op == 0xEB) return i + 1;
    if (op == 0xF5 || op == 0xF8 || op == 0xF9 || op == 0xFC || op == 0xFD || op == 0x9C ||
        op == 0x9D || op == 0x98 || op == 0x99 || op == 0x90) return i;      /* one byte */
    if (op == 0x0F) {                                    /* SSE moves and compares */
        if (i >= 8) return 0;
        op = code[i++];
        if (op >= 0x28 && op <= 0x2F) {
            if (i >= 8) return 0;
            modrm = code[i++];
            mod = (modrm >> 6) & 3;
            rm = modrm & 7;
            if (mod != 3) {
                if (rm == 4) i++;
                if (mod == 1) i += 1;
                else if (mod == 2) i += 4;
                else if (rm == 5) { i += 4; g_insn_rip = 1; }
            }
            return i;
        }
        return 0;
    }
    if (op == 0x6A) return i + 1;
    if (op == 0x68) return i + 4;
    if (op == 0x8D || op == 0x89 || op == 0x8B || op == 0x31 || op == 0x33 || op == 0x85 ||
        op == 0x3B || op == 0x01 || op == 0x03 || op == 0x29 || op == 0x2B || op == 0x39 ||
        op == 0x83 || op == 0x81 || op == 0xC7 || op == 0xFF || op == 0xF7 || op == 0x2D ||
        op == 0xC1 || op == 0xC0 || op == 0xD3 ||
        op == 0x05) {
        if (i >= 8) return 0;
        modrm = code[i++];
        mod = (modrm >> 6) & 3;
        rm = modrm & 7;
        if (mod != 3) {
            if (rm == 4) { i++; }                            /* sib */
            if (mod == 1) disp = 1;
            else if (mod == 2) disp = 4;
            else if (rm == 5) { disp = 4; g_insn_rip = 1; }  /* rip-relative */
        }
        i += disp;
        if (op == 0x83 || op == 0xC1 || op == 0xC0) imm = 1;
        else if (op == 0x81 || op == 0xC7 || op == 0xF7) imm = 4;
        else if (op == 0x05) imm = 4;
        return i + imm;
    }
    return 0;
}

/* A CALL, not a jump, is what makes this safe. `call target` pushes a return address, so the
   callee sees the same stack shape as a real call (8 mod 16 at entry) and its own `ret` returns
   to target+5 - which is why the first instruction has to be exactly 5 bytes long. If it is not,
   the hook is skipped and the caller falls back to installing from the injected thread.
     target : E8 rel32 -> stub, then NOP padding out to the instruction boundary
     stub   : pop our own return address (it is not needed - the jmp back takes over), then
              save rax/rcx/rdx/r8/r9, call hook_thunk, restore, jmp replay
     replay : the displaced instructions, then E9 rel32 -> target+displaced
   Popping the return address is what makes the frame identical to a normal entry: the callee
   later returns straight to the caller instead of back into our patch. The NOP padding only
   exists so the jump can always be five bytes. */
/* Displaced instructions are replayed at a different address, so every relative branch in them
   has to be re-encoded: an ILT thunk's body is nothing but `jmp rel32`, and copied verbatim it
   lands a quarter of the image away, in unmapped memory. Rel8 and rel32 jumps/calls only; a
   rip-relative memory operand still needs the address itself and is reported as unsupported. */
static int fix_replay(unsigned char *at, unsigned char *replay, int displaced)
{
    int off = 0;
    while (off < displaced) {
        int len = sub_insn_len(at + off);
        int p = 0, op, rel = 0, width = 0;
        if (!len) return 0;
        if (g_insn_rip) {
            /* the absolute target is the same, so recompute the displacement from the replay copy */
            signed long long absolute;
            int fresh;
            memcpy(&rel, at + off + len - 4, 4);
            absolute = (signed long long)(at + off + len) + rel;
            fresh = (int)(absolute - (signed long long)(replay + off + len));
            memcpy(replay + off + len - 4, &fresh, 4);
        }
        if (!len) return 0;
        while (p < 3 && (at[off + p] == 0x66 || at[off + p] == 0x67
                         || at[off + p] == 0xF2 || at[off + p] == 0xF3)) p++;
        if (at[off + p] >= 0x40 && at[off + p] <= 0x4F) p++;
        op = at[off + p];
        if (op == 0xE8 || op == 0xE9 || op == 0xEB) width = (op == 0xEB) ? 1 : 4;
        else if (op == 0x0F && (at[off + p + 1] & 0xF0) == 0x80) { width = 4; p++; }
        else if (op >= 0x70 && op <= 0x7F) width = 1;
        if (width) {
            /* displacement sits at the end of the instruction */
            if (width == 4) {
                signed long long target;
                int fresh;
                memcpy(&rel, at + off + len - 4, 4);
                target = (signed long long)(at + off + len) + rel;
                fresh = (int)(target - (signed long long)(replay + off + len));
                memcpy(replay + off + len - 4, &fresh, 4);
            }
            else {
                {
                    signed long long target_rel8_holder;
                    int rel8 = (unsigned char)at[off + len - 1];
                    signed long long target;
                    int fresh;
                    if (rel8 & 0x80) rel8 -= 0x100;
                    target = (signed long long)(at + off + len) + rel8;
                    target_rel8_holder = target;
                    fresh = (int)(target_rel8_holder - (signed long long)(replay + off + len));
                    if (fresh > 127 || fresh < -128) return 0;
                    replay[off + len - 1] = (unsigned char)fresh;
                }
            }
        }
        off += len;
    }
    return 1;
}

/* ---------------- remote patching ----------------
   Arbitrary Code Guard also refuses the patch itself: VirtualProtect(Ex) answers 5 and even
   WriteProcessMemory(GetCurrentProcess()) answers 998 on a .text page, so the client's code cannot
   be written from inside at all. The gui can, from outside, so a hook asks it over the pipe, gets
   the bytes back acked, and the client never touches its own text. */

static int remote_patch(void *at, const unsigned char *bytes, int length)
{
    /* Only a file request, no round trip: waiting for the gui blocks the client's own vm thread,
       and the gui applies the bytes from outside within a fraction of a second. A patch that
       lands late is harmless - the prologue is simply still the original until then. */
    unsigned char *client = module_base(L"RobloxPlayerBeta.exe");
    char path[MAX_PATH];
    char line[160];
    char *o;
    HANDLE file;
    DWORD wrote;
    long rva;
    int i;

    if (!client) return 0;
    rva = (long)((unsigned char *)at - client);
    if (length > 24) return 0;
    workspace(path);
    {
        char tail[32];
        wsprintfA(tail, "\\patch-%u.txt", GetCurrentProcessId());
        lstrcatA(path, tail);
    }
    o = line;
    {
        char head[24];
        wsprintfA(head, "%lx ", rva);
        for (char *h = head; *h; h++) *o++ = *h;
    }
    for (i = 0; i < length; i++) {
        const char *digits = "0123456789abcdef";
        *o++ = digits[bytes[i] >> 4];
        *o++ = digits[bytes[i] & 15];
    }
    *o++ = 10;
    *o = 0;
    file = CreateFileA(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, 0, OPEN_ALWAYS,
                       FILE_ATTRIBUTE_NORMAL, 0);
    if (file == INVALID_HANDLE_VALUE) { put("rp no file"); return 0; }
    WriteFile(file, line, (DWORD)lstrlenA(line), &wrote, 0);
    CloseHandle(file);
    return 1;
}

/* Arbitrary Code Guard refuses the Win32 call on image pages, so the NT form is used first: it
   is the same operation with the policy check done by the memory manager instead of the loader. */
static int protect_code(void *at, size_t size, DWORD protect, DWORD *old)
{
    typedef long (NTAPI *nt_protect_t)(void *, void **, ULONG_PTR *, ULONG, ULONG *);
    nt_protect_t nt;
    void *base = at;
    ULONG_PTR span = size;
    ULONG was = 0;
    long status;
    nt = (nt_protect_t)GetProcAddress(GetModuleHandleA("ntdll.dll"), "NtProtectVirtualMemory");
    if (nt) {
        status = nt(GetCurrentProcess(), &base, &span, protect, &was);
        if (status == 0) { if (old) *old = was; return 1; }
    }
    return VirtualProtect(at, size, protect, old) ? 1 : 0;
}

static int install_hook(void *target)
{
    DWORD old;
    unsigned char *at = (unsigned char *)target;
    unsigned char *stub;
    int i, n = 0, displaced = 0, guard = 0;
    ptrdiff_t delta;

    unsigned char head[32];

    if (!target) return 0;
    /* The target lives in Hyperion's text: one guarded read lets their handler decrypt the page,
       then the prologue is decoded from a copy. An unreadable prologue is a skipped hook, never a
       dead client. */
    if (!decrypt_touch(at)) { put("ih no touch"); return 0; }
    if (!safe_read(at, head, sizeof(head))) { put("ih no read"); return 0; }
    if (!head[0] && !head[1]) { put("ih zero page"); return 0; }

    /* whole instructions, at least the twelve the absolute jump needs */
    while (displaced < 12) {
        int length = sub_insn_len(head + displaced);
        if (!length) {
            const char *hexd = "0123456789abcdef";
            char line[64];
            char *o = line;
            const char *q = "ih bad insn ";
            while (*q) *o++ = *q++;
            for (int k = 0; k < 8; k++) { *o++ = hexd[head[displaced + k] >> 4];
                                          *o++ = hexd[head[displaced + k] & 15]; }
            *o++ = '@';
            *o++ = hexd[(int)((unsigned __int64)(at + displaced) >> 60) & 15];
            *o++ = hexd[(int)((unsigned __int64)(at + displaced) >> 56) & 15];
            *o = 0;
            put(line);
            return 0;
        }
        displaced += length;
        if (++guard > 10) { put("ih long prologue"); return 0; }
    }
    if (displaced > 12) displaced = displaced;   /* keep, the jump fills what it needs */

    if (!g_stubsReady) {
        g_stubsReady = 1;
        put("ih stubs ready");
#if !STUB_ARENA_OK
        if (!VirtualProtect(g_stubs, sizeof(g_stubs), PAGE_EXECUTE_READWRITE, &wasArena)) {
            put("ih arena not executable");
            return 0;
        }
#endif
    }
    g_trampoline = g_stubs;

    /* Every hook needs its own stub and replay: reusing one page means the second install
       overwrites the first hook's displaced bytes, so the first target then replays the
       second target's prologue and tail-jumps into the wrong function. */
    if (!g_hookArena) g_hookArena = g_trampoline;
    if (g_hookArena + HOOK_SLOT > g_trampoline + 4096) { put("ih arena full"); return 0; }
    if (displaced > (int)sizeof(g_hooks[0].orig)) { put("ih too far"); return 0; }

    /* already hooked there? rebuild from the saved prologue instead of the live bytes */
    for (i = 0; i < g_hookCount; i++)
        if (g_hooks[i].target == target) break;
    if (i < g_hookCount) {
        stub = g_trampoline + HOOK_SLOT * i;
        g_replay = stub + 64;
        displaced = g_hooks[i].displaced;
        for (n = 0; n < displaced; n++) g_replay[n] = g_hooks[i].orig[n];
        n = displaced;
    }
    else {
        if (g_hookCount >= HOOK_SLOTS) return 0;
        stub = g_hookArena;
        g_hookArena += HOOK_SLOT;
        g_replay = stub + 64;
        g_hooks[g_hookCount].target = target;
        g_hooks[g_hookCount].displaced = displaced;
        for (n = 0; n < displaced; n++) g_hooks[g_hookCount].orig[n] = head[n];
        g_hookCount++;
        for (n = 0; n < displaced; n++) g_replay[n] = head[n];
    }
    if (!fix_replay(at, g_replay, displaced)) { put("ih replay"); return 0; }
    n = 0;
    g_replay[displaced] = 0x48;                          /* mov rax, at+displaced */
    g_replay[displaced + 1] = 0xB8;
    *(unsigned long long *)&g_replay[displaced + 2] = (unsigned long long)(at + displaced);
    g_replay[displaced + 10] = 0x50;                     /* push rax */
    g_replay[displaced + 11] = 0xC3;                     /* ret, back into the function */

    if (HOST_ONLY(g_noThunk)) {                                      /* bisect: replay only */
        n = 0;
        stub[n++] = 0x48;                                /* mov rax, g_replay */
        stub[n++] = 0xB8;
        *(unsigned long long *)&stub[n] = (unsigned long long)g_replay;
        n += 8;
        stub[n++] = 0x50;                                /* push rax */
        stub[n++] = 0xC3;                                /* ret */
        goto patch_target;
    }
    stub[n++] = 0x50;                                     /* push rax */
    stub[n++] = 0x51;                                     /* push rcx */
    stub[n++] = 0x52;                                     /* push rdx */
    stub[n++] = 0x41; stub[n++] = 0x50;                   /* push r8  */
    stub[n++] = 0x41; stub[n++] = 0x51;                   /* push r9  */
    /* The callee owns the 32 bytes below rsp as shadow space. Without reserving them the thunk
       spills straight over the five saved registers and the pops restore garbage, so they have
       to be subtracted too (40 + 32 also keeps the call 16-byte aligned). */
    /* The patch is an absolute jmp, not a call, so no return address sits on the stack: entry rsp
       is 16-aligned and has to be re-aligned for the call. 40 pushed + 8 pad + 32 shadow = 0x28,
       which leaves rsp 16-aligned before the call and 8-aligned inside the thunk, as the abi
       expects. Without the pad every movaps spill in the thunk faults. */
    stub[n++] = 0x48; stub[n++] = 0x83; stub[n++] = 0xEC; stub[n++] = 0x28;  /* sub rsp, 0x28 */
    stub[n++] = 0x48; stub[n++] = 0xB8;                   /* mov rax, hook_thunk */
    *(unsigned long long *)&stub[n] = (unsigned long long)hook_thunk;
    n += 8;
    stub[n++] = 0xFF; stub[n++] = 0xD0;                   /* call rax */
    stub[n++] = 0x48; stub[n++] = 0x83; stub[n++] = 0xC4; stub[n++] = 0x28;  /* add rsp, 0x28 */
    stub[n++] = 0x41; stub[n++] = 0x59;                   /* pop r9  */
    stub[n++] = 0x41; stub[n++] = 0x58;                   /* pop r8  */
    stub[n++] = 0x5A;                                     /* pop rdx */
    stub[n++] = 0x59;                                     /* pop rcx */
    stub[n++] = 0x58;                                     /* pop rax */
    stub[n++] = 0x48;                                    /* mov rax, g_replay */
    stub[n++] = 0xB8;
    *(unsigned long long *)&stub[n] = (unsigned long long)g_replay;
    n += 8;
    stub[n++] = 0x50;                                    /* push rax */
    stub[n++] = 0xC3;                                    /* ret: not cfg-checked, jmp rax is */
patch_target:
    FlushInstructionCache(GetCurrentProcess(), g_trampoline, HOOK_SLOTS * HOOK_SLOT);

#ifdef LYPHEN_HOST
    printf("  [dbg] install at=%p displaced=%d stub=%p replay=%p rel=%lld\n", (void *)at,
           displaced, (void *)stub, (void *)g_replay, (long long)(stub - (at + 5)));
#endif
    delta = 0;
    {
        unsigned char patch[16];
        DWORD was = 0;
        MEMORY_BASIC_INFORMATION mbi;
        char dbg[96];
        int how = 0;
        DWORD nowProt = 0;
        if (VirtualQuery(at, &mbi, sizeof(mbi))) nowProt = (DWORD)mbi.Protect;

        patch[0] = 0x48;                                 /* mov rax, stub */
        patch[1] = 0xB8;
        *(unsigned long long *)&patch[2] = (unsigned long long)stub;
        patch[10] = 0xFF;                                /* jmp rax */
        patch[11] = 0xE0;
        for (i = 12; i < displaced; i++) patch[i] = 0x90;

        /* Only PAGE_EXECUTE_WRITECOPY is granted under code integrity, and the page has to be put
           back to what it was afterwards or the client later executes a copy-on-write page it does
           not expect. When even that is refused the request goes to the gui. */
        if (protect_code(at, displaced + 8, PAGE_EXECUTE_READWRITE, &was)) {
            memcpy(at, patch, displaced);
            protect_code(at, displaced + 8, was ? was : PAGE_EXECUTE_READ, 0);
            how = 1;
        }
        else if (remote_patch(at, patch, displaced)) {
            how = 3;
            was = 0;
        }
        wsprintfA(dbg, "ih patched %p prot %lx how %d was %lx", at, nowProt, how, was);
        put(dbg);
        if (!how) { put("ih no patch"); return 0; }
    }
#ifdef LYPHEN_HOST
    { extern int printf(const char *fmt, ...);
      printf("  [dbg] at=%p stub=%p wrote=%llx slot=%llu\n", (void *)at, (void *)stub,
             *(unsigned long long *)(at + 2),
             (unsigned long long)((unsigned long long)(stub - g_stubs) / HOOK_SLOT)); }
#endif
    FlushInstructionCache(GetCurrentProcess(), at, displaced);
    g_hooked = 1;
    return 1;
}

/* installed on the VM thread: load the runner, then the hook is no longer needed */
/* The gui finds the state and the loadstring closure from outside (it can read the whole heap
   safely, with no traps) and leaves them here as "%x %x"; the payload only parses it back. */
static lua_State *state_from_file(void)
{
    char path[MAX_PATH], text[64];
    HANDLE file;
    DWORD read = 0;
    unsigned long long values[3] = { 0, 0, 0 };
    int field = 0, digits = 0;
    GetEnvironmentVariableA("LOCALAPPDATA", path, MAX_PATH);
    lstrcatA(path, "/Lyphen/state.txt");
    file = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, 0, OPEN_EXISTING, 0, 0);
    if (file == INVALID_HANDLE_VALUE) return 0;
    if (!ReadFile(file, text, sizeof(text) - 1, &read, 0)) { CloseHandle(file); return 0; }
    CloseHandle(file);
    text[read] = 0;
    for (DWORD i = 0; i < read && field < 3; i++) {
        char c = text[i];
        int nibble;
        if (c >= '0' && c <= '9') nibble = c - '0';
        else if (c >= 'a' && c <= 'f') nibble = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') nibble = c - 'A' + 10;
        else { if (digits) { field++; digits = 0; } continue; }
        values[field] = (values[field] << 4) | (unsigned)nibble;
        digits++;
    }
    if (!values[0]) return 0;
    g.loadstringClosure = (void *)values[1];
    g.printClosure = (void *)values[2];
    return (lua_State *)values[0];
}

/* The trigger's first argument *is* the state: tolstring, pcall and gettop all take L in rcx, so
   a hook on one of them hands it over and the blind heap crawl is only a fallback. */
static int plausible_state(lua_State *state)
{
    unsigned char head[0x90];
    unsigned long long ci, base, top, global;
    if (!state || ((ULONG_PTR)state & 7)) return 0;
    if (!safe_read(state, head, sizeof(head))) return 0;
    ci = *(unsigned long long *)(head + L_CI);
    base = *(unsigned long long *)(head + L_BASE);
    top = *(unsigned long long *)(head + L_TOP);
    global = *(unsigned long long *)(head + L_GLOBAL);
    if (global < 0x10000 || ci < 0x10000 || base < 0x10000) return 0;
    if (top < base || top - base > 0x800000) return 0;
    return 1;
}

static void install_now(void *hint)
{
    lua_State *state;
    g_installed = 1;
    if (hint && plausible_state((lua_State *)hint)) {
        put("state from hook arg");
        g.state = (lua_State *)hint;
    }
    if (!g.state) {
        lua_State *given = state_from_file();
        if (given && plausible_state(given)) {
            put("state from file");
            g.state = given;
        }
    }
    state = g.state ? g.state : state_from_scriptcontext();
    if (!state) {
        g_installed = 0;
        return;
    }
    g.state = state;
    emit("i", 0, "on the vm thread, installing runner");
    /* proof of life that needs no table api: compile a chunk through the base library's own
       loadstring, run it, and log whatever comes back. The bridge needs lua_createtable and
       friends, so this is the only path that survives when only the pinned four resolve. */
    if (state && g.luaBpcall && g.loadstringClosure) {
        char message[256];
        const char *args[2];
        message[0] = 0;
        args[0] = "return 40 + 2";
        args[1] = "@Lyphen/smoke";
        if (g.printClosure) {
            char probeMsg[128];
            probeMsg[0] = 0;
            put("p1 probe print");
            if (call_borrowing_frame(state, g.printClosure, 0, 0, probeMsg, sizeof(probeMsg)) != 2)
                put("p2 probe ok");
            else
                fail("resume", probeMsg[0] ? probeMsg : "probe print failed");
        }
        put("s2 loadstring");
        if (call_borrowing_frame(state, g.loadstringClosure, args, 2, message, sizeof(message)) == 2
            || top_tag(state) != 7) {
            fail("resume", message[0] ? message : "loadstring gave no closure");
        } else {
            void *closure = *(void **)(*(unsigned char **)((unsigned char *)state + L_TOP) - 0x10);
            put("s3 compiled");
            if (call_borrowing_frame(state, closure, 0, 0, message, sizeof(message)) != 2)
                put("smoke ran clean");
            else
                fail("resume", message[0] ? message : "chunk failed");
        }
    }
    spawn_runner(state, g_apiSource, g_runnerSource);
}

/* ------------------------------------------------------------------ entry */
static int g_guardReady;
static DWORD run_entry(void);

/* A manual map is not a module, so the loader never sees our .pdata and every __try in the payload
   would be dead weight: an access violation anywhere would kill the client instead of landing in
   the handler. Registering our own unwind table is what makes the SEH real. */
extern IMAGE_DOS_HEADER __ImageBase;
typedef BOOLEAN (WINAPI *rtl_add_table_t)(PRUNTIME_FUNCTION, DWORD, DWORD64);

static int register_unwind(void)
{
    unsigned char *base = (unsigned char *)&__ImageBase;
    IMAGE_NT_HEADERS *nt;
    rtl_add_table_t add;
    DWORD rva, size;
    put("u1 base");
    if (__ImageBase.e_magic != IMAGE_DOS_SIGNATURE) return 0;
    nt = (IMAGE_NT_HEADERS *)(base + __ImageBase.e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return 0;
    rva = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXCEPTION].VirtualAddress;
    size = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXCEPTION].Size;

    put("u2 dir");
    if (!rva || size < sizeof(RUNTIME_FUNCTION)) return 0;
    add = (rtl_add_table_t)GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "RtlAddFunctionTable");
    if (!add) return 0;
    put("u3 add");
    return add((PRUNTIME_FUNCTION)(base + rva), size / sizeof(RUNTIME_FUNCTION),
               (DWORD64)(ULONG_PTR)base) ? 1 : 0;
}

__declspec(dllexport) DWORD WINAPI lyphen_entry(void *parameter)
{
    DWORD code = 0;
    (void)parameter;
    LYPHEN_TRY {
        code = run_entry();
    }
    /* take the C++ throw as well: Luau only throws from api_check, so it means our call shape was
       wrong. Logging it and dying are very different debugging experiences. */
    LYPHEN_EXCEPT(EXCEPTION_EXECUTE_HANDLER) {
        char line[160];
        EXCEPTION_POINTERS *info = GetExceptionInformation();
        wsprintfA(line, "fail entry %s code %lx at %p", g_stage, GetExceptionCode(),
                  info->ExceptionRecord->ExceptionAddress);
        put(line);
        emit("e", 0, line);
    }
    return code;
}

static DWORD run_entry(void)
{
    char path[MAX_PATH];
    char name[64];
    HANDLE file;
    bus_header *header;

    put("m1 enter");
    {
        char stopPath[MAX_PATH];
        workspace(stopPath);
        lstrcatA(stopPath, "\\stop.txt");
        {
            HANDLE h = CreateFileA(stopPath, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, 0,
                                   OPEN_EXISTING, 0, 0);
            char num[8] = { 0 };
            DWORD got = 0;
            if (h != INVALID_HANDLE_VALUE) {
                ReadFile(h, num, sizeof(num) - 1, &got, 0);
                CloseHandle(h);
                for (DWORD k = 0; k < got && num[k] >= '0' && num[k] <= '9'; k++)
                    g_stopAt = g_stopAt * 10 + (num[k] - '0');
                if (got && (num[0] == 'd' || num[0] == 'D')) g_stopAt = -1;
            }
        }
    }
    if (g_started) return 0;
    if (g_stopAt == 55) { put("early return"); return 0; }
    g_started = 1;
    put(register_unwind() ? "m2 unwind ok" : "m2 no unwind");
#ifndef LYPHEN_HOST
    /* a manual map never runs DllMain, so the CFG thunks have to come up here */
    if (!g_guardReady) { g_guardReady = 1; init_guard_thunks(); }
#endif
    stage("bus");
    GetEnvironmentVariableA("LOCALAPPDATA", path, MAX_PATH);
    lstrcatA(path, "\\Lyphen");
    CreateDirectoryA(path, 0);
    wsprintfA(name, "\\bus-%lu.bin", GetCurrentProcessId());
    lstrcatA(path, name);

    file = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                       0, OPEN_ALWAYS, 0, 0);
    if (file == INVALID_HANDLE_VALUE) { char m[128]; wsprintfA(m, "dbg bus file err %lu", GetLastError()); put(m); }
    if (file != INVALID_HANDLE_VALUE) {
        HANDLE section = CreateFileMappingA(file, 0, PAGE_READWRITE, 0, BUS_SIZE, 0);
        if (section) {
            g.bus = MapViewOfFile(section, FILE_MAP_WRITE | FILE_MAP_READ, 0, 0, BUS_SIZE);
            CloseHandle(section);
        }
        CloseHandle(file);
    }
    put(path);
    if (!g.bus) {
        put("m3 bus fail");
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

    put("m4 bus ok");
    stage("pipe");
    if (!open_pipe()) { put("m5 pipe fail"); emit("e", 0, "pipe: no gui could connect"); }
    else put("m5 pipe ok");

    put("m6 pipe done");
    stage("scan");
    if (!resolve()) {
        emit("e", 0, "resolve failed, see the scan stage");
    }

    put("m7 resolved");
    stage("state");
    g.state = state_from_file();          /* the gui leaves it here; empty file = 0 */
    if (g.state) put("sfile");
    stage("s1");
    /* The hook supplies the state, so the heap crawl is skipped here: it is the one step that
       reliably kills the client, and nothing needs its answer until the fallback path. */
    {
        g_apiSource = read_chunk("api.luau");
        g_runnerSource = read_chunk("runner.luau");
        stage("s2");
        if (!g_runnerSource) fail("compile", "runner.luau missing from %LOCALAPPDATA%\\Lyphen");
        /* gettop is the trigger of choice: it is called constantly, it never throws, and it has no
           frame of its own, so replaying its prologue cannot disturb error handling. The trampoline
           page carries no unwind info, so a throwing function is the wrong place to stand in. */
        /* stop.txt holding 'd' installs straight from the injected thread: nothing in a live
           client calls the base library closures, so the hook trigger never fires */
        else if (g_stopAt < 0) {
            put("direct install");
            install_now(0);
        }
        else if (put("m8 runner read"),
                 (put("h1"), g.luaBpcall && install_hook((void *)g.luaBpcall)) ||
                 (put("h2"), g.luaBprint && install_hook((void *)g.luaBprint)) ||
                 (put("h3"), g.tolstring && install_hook((void *)g.tolstring)))
            emit("i", 0, "waiting for the vm thread");
        else {
            /* no hook possible: fall back to installing from here, which is the racy path */
            emit("w", 0, "no hook, installing from the injected thread");
            install_now(0);
        }
    }

    stage("s3");
    put("e2 block done");
    emit("i", 0, "core up");
    put("e3 emitted");
    stage("s4");
    put("entry ret");
    /* no loop here: the caller's thread is given straight back. Everything after this runs on
       the runner's Heartbeat (bridge pump), so we add no thread to the client. */
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE self, DWORD reason, LPVOID reserved)
{
    (void)self;
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
#ifndef LYPHEN_HOST
        init_guard_thunks();
#endif
        /* the host calls lyphen_entry through the hijacked thread, not from here: DllMain
           has a loader lock and every scan would be riskier inside it */
        stage("attach");
    }
    return TRUE;
}
