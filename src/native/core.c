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
#include <winhttp.h>
#include "patterns.h"

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
#define OUT_ERROR_ID 3

/* measured 2026-11-24 on 2.748.1526.0 (docs: ASSUMPTIONS.md) */
#define L_TOP 0x80
#define L_BASE 0x78
#define L_CI 0x58
#define L_GLOBAL 0x68

#ifndef UMBRA_HOST
/* in the shipped build there is no lua.h: the state is opaque and the API arrives as
   function pointers from the scan. The harness includes the real header instead. */
typedef void *lua_State;
#endif
typedef int (*luau_load_t)(lua_State *, const char *, const char *, size_t, int);
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
static int resolve(void);
static int calibrate(unsigned char *base);
static int open_pipe(void);
static char g_stage[32] = "start";

/* ------------------------------------------------------------------ plumbing */
/* safe read: MSVC uses SEH, mingw (whose SEH is a no-op) reads through the kernel instead.
   Everything that can point at a wrong address goes through this. */
static int safe_read(const void *at, void *out, size_t size)
{
#if defined(__MINGW32__)
    SIZE_T got = 0;
    return ReadProcessMemory(GetCurrentProcess(), at, out, size, &got) && got == size;
#else
    UMBRA_TRY {
        const unsigned char *from = (const unsigned char *)at;
        unsigned char *to = (unsigned char *)out;
        for (size_t i = 0; i < size; i++) to[i] = from[i];
        return 1;
    }
    UMBRA_EXCEPT(EXCEPTION_EXECUTE_HANDLER) {
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
                void *candidate = 0;
                if (globalState < 0x10000 || (globalState & 7)) continue;
                if (!safe_read((void *)((ULONG_PTR)globalState + 0x2f0), &candidate, sizeof(candidate)))
                    continue;
                if (candidate && !((ULONG_PTR)candidate & 7) && looks_resting((lua_State *)candidate))
                    return (lua_State *)candidate;
            }
        }
        cursor = (unsigned char *)region.BaseAddress + region.RegionSize;
    }
    return 0;
}

/* source -> bytecode -> closure. Returns the same codes as luau_load so callers stay simple. */
static int load_source(lua_State *state, const char *chunkname, const char *source)
{
    size_t size = 0;
    char *bytecode;
    int status;
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
    if (!state || !g.load || !g.pcall) {
        fail("resume", "no state or no luau entry");
        return;
    }
    if (!g.createtable || !g.pushcclosure || !g.setfield || !g.rawseti) {
        fail("resume", "table api unresolved (bridges cannot be built)");
        return;
    }
    if (!chunk) {
        fail("compile", "runner.luau missing");
        return;
    }
    {
        /* function first, then its arguments: pcall takes the callee from
           L->top - (nargs + 1), so anything pushed before the callee would be called instead.
           stack: [runner] [bridge] [bus] -> pcall(2) -> runner(bridge, bus) */
        if (load_source(state, "@Umbra/runner", chunk) != 0) {
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
    lstrcatA(path, "\\Umbra\\workspace");
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

    session = WinHttpOpen(L"Umbra/0.1", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
                          WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session) return failarg(state, "winhttp open"), 3;
    connect = WinHttpConnect(session, wideUrl, port, 0);
    if (!connect) { WinHttpCloseHandle(session); return failarg(state, "connect"), 3; }

    {   wchar_t widePath[1024];
        MultiByteToWideChar(CP_ACP, 0, path, -1, widePath, 1024);
        request = WinHttpOpenRequest(connect, wideMethod, widePath, 0,
                                     WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                     secure ? WINHTTP_FLAG_SECURE : 0);
    }
    if (!request) {
        WinHttpCloseHandle(connect); WinHttpCloseHandle(session);
        return failarg(state, "request"), 3;
    }
    if (type[0]) WinHttpAddRequestHeaders(request, wideType, -1, WINHTTP_ADDREQ_FLAG_ADD);

    if (WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                           body[0] ? body : WINHTTP_NO_REQUEST_DATA, lstrlenA(body), lstrlenA(body), 0) &&
        WinHttpReceiveResponse(request, 0)) {
        DWORD code = 0, size = sizeof(code);
        WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
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
            if (!WinHttpReadData(request, chunk, 4096, &read) || read == 0) break;
            total += read;
        }
        g.pushls(state, all ? all : "", all ? total : 0);
        if (all) VirtualFree(all, 0, MEM_RELEASE);
    }
    if (g.setfield) g.setfield(state, -2, "body");

    WinHttpCloseHandle(request);
    WinHttpCloseHandle(connect);
    WinHttpCloseHandle(session);
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
    if (!text || !g.compile || !g.load) return failarg(state, "loadstring unavailable"), 2;
    source = (char *)VirtualAlloc(0, length + 1, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!source) return failarg(state, "no memory"), 2;
    for (size_t i = 0; i < length; i++) source[i] = text[i];
    source[length] = 0;
    bytecode = g.compile(source, length, 0, &size);
    VirtualFree(source, 0, MEM_RELEASE);
    if (!bytecode || size == 0) return failarg(state, "compile failed"), 2;
    if (g.load(state, "@Umbra/chunk", bytecode, size, 0) != 0) return 1;   /* nil, err */
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
typedef struct {
    LIST_ENTRY order;
    LIST_ENTRY links;
    unsigned long long base;
    unsigned long long entry;
    unsigned long size;
    unsigned short nameLength;
    unsigned short nameMax;
    wchar_t *name;
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
    LIST_ENTRY *head, *cursor;
    UMBRA_TRY {
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
    UMBRA_EXCEPT(EXCEPTION_EXECUTE_HANDLER) {
        return 0;
    }
    return 0;
}

static int executable(void *address)
{
    MEMORY_BASIC_INFORMATION region;
    if (!VirtualQuery(address, &region, sizeof(region))) return 0;
    return region.State == MEM_COMMIT &&
           (region.Protect == PAGE_EXECUTE_READ || region.Protect == PAGE_EXECUTE_READWRITE ||
            region.Protect == PAGE_EXECUTE_WRITECOPY || region.Protect == PAGE_EXECUTE);
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
    if (rva > UMBRA_TEXT_SIZE) {
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
    g.field = (type)at(base, UMBRA_RVA_##suffix, \
                      UMBRA_SIGLEN_##suffix ? UMBRA_SIG_##suffix : 0, \
                      UMBRA_SIGLEN_##suffix, label)

static int resolve(void)
{
    unsigned char *base = module_base(L"RobloxPlayerBeta.exe");
    int ok = 1;
    if (!base) {
        fail("scan", "RobloxPlayerBeta.exe is not loaded");
        return 0;
    }
    RESOLVE(load, luau_load_t, LUAU_LOAD, "luau_load");
    RESOLVE(pcall, lua_pcall_t, LUA_PCALL, "lua_pcall");
    RESOLVE(pushcclosure, lua_pushcclosure_t, LUA_PUSHCCLOSURE, "lua_pushcclosure");
    RESOLVE(createtable, lua_createtable_t, LUA_CREATETABLE, "lua_createtable");
    RESOLVE(setfield, lua_setfield_t, LUA_SETFIELD, "lua_setfield");
    RESOLVE(rawseti, lua_rawseti_t, LUA_RAWSETI, "lua_rawseti");
    RESOLVE(pushls, lua_pushlstring_t, LUA_PUSHLSTRING, "lua_pushlstring");
    RESOLVE(pushnumber, lua_pushnumber_t, LUA_PUSHNUMBER, "lua_pushnumber");
    RESOLVE(pushboolean, lua_pushboolean_t, LUA_PUSHBOOLEAN, "lua_pushboolean");
    RESOLVE(tolstring, lua_tolstring_t, LUA_TOLSTRING, "lua_tolstring");
    RESOLVE(tonumber, lua_tonumber_t, LUA_TONUMBER, "lua_tonumber");
    RESOLVE(gettop, lua_gettop_t, LUA_GETTOP, "lua_gettop");

    /* nothing measured yet (the shipped text is packed, so these are normally all zero):
       calibrate in memory and keep whatever worked */
    if (!g.load || !g.pcall) {
        if (!calibrate(base)) emit("e", 0, "calibration failed, the executor stays dumb");
        g.compile = 0;   /* discovered lazily below */
    }
    ok = g.load && g.pcall && g.pushcclosure && g.createtable && g.setfield && g.rawseti;
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
    lstrcatA(path, "\\Umbra\\");
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
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt;
    IMAGE_SECTION_HEADER *section;
    int i;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    section = IMAGE_FIRST_SECTION(nt);
    for (i = 0; i < nt->FileHeader.NumberOfSections; i++, section++) {
        unsigned char *at, *end;
        if (!(section->Characteristics & IMAGE_SCN_MEM_READ)) continue;
        if (section->Characteristics & IMAGE_SCN_MEM_EXECUTE) continue;   /* packed */
        at = base + section->VirtualAddress;
        end = at + section->Misc.VirtualSize - size;
        for (; at < end; at++) {
            char window[32];
            if (from && at < (unsigned char *)from) continue;
            if (!safe_read(at, window, size)) continue;
            if (!memcmp(window, name, size) && at[size] == 0 && is_name(at, size + 1, 32))
                return (char *)at;
        }
    }
    return 0;
}

/* rip-relative references to a fixed address, in executable memory only */
static void *find_reference(void *needle, size_t limit)
{
    SYSTEM_INFO info;
    unsigned char *cursor;
    size_t seen = 0;
    GetSystemInfo(&info);
    cursor = (unsigned char *)info.lpMinimumApplicationAddress;
    while (cursor < (unsigned char *)info.lpMaximumApplicationAddress && seen < limit) {
        MEMORY_BASIC_INFORMATION region;
        unsigned char *at, *end;
        if (!VirtualQuery(cursor, &region, sizeof(region))) break;
        cursor = (unsigned char *)region.BaseAddress + region.RegionSize;
        if (region.State != MEM_COMMIT) continue;
        if (region.Protect != PAGE_EXECUTE_READ && region.Protect != PAGE_EXECUTE_READWRITE &&
            region.Protect != PAGE_EXECUTE_WRITECOPY && region.Protect != PAGE_EXECUTE)
            continue;
        at = (unsigned char *)region.BaseAddress;
        end = at + region.RegionSize - 8;
        seen += region.RegionSize;
        for (; at < end; at++) {
            unsigned char head[8];
            long displacement;
            unsigned char *target;
            if (!safe_read(at, head, sizeof(head))) break;
            if ((head[0] & 0xF8) != 0x48) continue;              /* REX.W */
            /* lea r64,[rip+d] takes the address, mov r64,[rip+d] loads a slot holding it */
            if (head[1] != 0x8D && head[1] != 0x8B) continue;
            if ((head[2] & 0xC7) != 0x05) continue;              /* [rip + disp32] */
            displacement = *(long *)&head[3];
            target = (unsigned char *)at + 7 + displacement;
            if (target == (unsigned char *)needle) return at;
            if (head[1] == 0x8B) {
                void *slot = 0;
                if (safe_read(target, &slot, sizeof(slot)) && slot == needle) return at;
            }
        }
    }
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
#ifdef UMBRA_HOST
    { extern int printf(const char *, ...); printf("  [dbg] anchor %s base=%p hit=%p\n", name, (void *)base, (void *)hit); }
#endif
    while (hit && !chosen) {
        void *ref = find_reference(hit, 192u << 20);
        if (ref) {
            void *calls[24];
            int found = gather_calls((unsigned char *)ref, 320, calls, 24);
            int i;
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
static void *find_gettop(unsigned char *base)
{
    unsigned char *start = base + 0x1000;
    size_t span = UMBRA_TEXT_SIZE > (8u << 20) ? (8u << 20) : UMBRA_TEXT_SIZE;
    size_t i;
    for (i = 0; i + 32 < span; i++) {
        unsigned char code[32];
        if (!safe_read(start + i, code, sizeof(code))) return 0;
        /* 48 8B 41 80      mov rax,[rcx+0x80]
           48 2B 41 78      sub rax,[rcx+0x78]
           48 C1 F8 04      sar rax,4
           C3               ret */
        if (code[0] != 0x48 || code[1] != 0x8B || code[2] != 0x41 || code[3] != 0x80) continue;
        if (code[4] != 0x48 || code[5] != 0x2B || code[6] != 0x41 || code[7] != 0x78) continue;
        if (code[8] != 0x48 || code[9] != 0xC1 || code[10] != 0xF8 || code[11] != 0x04) continue;
        if (code[12] != 0xC3) continue;
        return start + i;
    }
    return 0;
}

/* Every lua_push* ends by advancing the stack pointer: add qword [rcx+0x80], 0x10, then ret.
   That is common to all of them, so they are told apart by what they store first. */
static void *find_push(int kind)
{
    unsigned char *base = module_base(L"RobloxPlayerBeta.exe");
    size_t i, span = 8u << 20;
    unsigned char *start;
    if (!base) return 0;
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

static void report_pick(const char *label, void *at)
{
    char line[96];
    char *o = line;
    const char *a = "calibrated ";
    static const char *digits = "0123456789abcdef";
    int i;
    while (*a) *o++ = *a++;
    while (*label) *o++ = *label++;
    *o++ = '=';
    for (i = 0; i < 16; i++) *o++ = digits[((unsigned long long)at >> (60 - 4 * i)) & 0xF];
    *o = 0;
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
    g.pcall = (lua_pcall_t)anchor_calls(base, "pcall", 5, 0);
    if (g.load) report_pick("load", (void *)g.load);
    else fail("scan", "no load anchor found");
    if (!g.compile) {
        /* the compile call sits immediately before the load call in luaB_loadstring */
        unsigned char *loadAt = (unsigned char *)g.load;
        g.compile = (luau_compile_t)previous_call(loadAt, 0x400);
    }

    if (!g.pcall) {
        /* the pcall entry often references it indirectly; fall back to the other top-writing
           callee in loadstring, which is the compile/push side */
        g.pcall = (lua_pcall_t)anchor_calls(base, "loadstring", 10, 0);
        if ((void *)g.pcall == (void *)g.load) g.pcall = 0;
    }
    if (g.pcall) report_pick("call", (void *)g.pcall);
    else fail("scan", "no call anchor found");
    if (!g.gettop && (g.gettop = (lua_gettop_t)find_gettop(base)) != 0)
        report_pick("gettop", (void *)g.gettop);
    if (g.compile) report_pick("compile", (void *)g.compile);
    if (!g.pushboolean && (g.pushboolean = (lua_pushboolean_t)find_push(1)) != 0)
        report_pick("pushbool", (void *)g.pushboolean);
    if (!g.pushnumber && (g.pushnumber = (lua_pushnumber_t)find_push(2)) != 0)
        report_pick("pushnum", (void *)g.pushnumber);
    if (!g.pushls && (g.pushls = (lua_pushlstring_t)find_push(3)) != 0)
        report_pick("pushstr", (void *)g.pushls);
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
    wsprintfA(name, "\\\\.\\pipe\\umbra-%u", GetCurrentProcessId());
    g.pipe = CreateNamedPipeA(name, PIPE_ACCESS_DUPLEX,
                              PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT,
                              1, 8192, 8192, 0, 0);
    if (g.pipe == INVALID_HANDLE_VALUE) return 0;
    /* NOWAIT: 535 means a client is already there, 536 means we are listening - both fine,
       and either way the connection completes by itself when the gui connects */
    if (ConnectNamedPipe(g.pipe, 0) == 0 && GetLastError() != ERROR_PIPE_CONNECTED &&
        GetLastError() != ERROR_PIPE_LISTENING) {
        CloseHandle(g.pipe);
        g.pipe = INVALID_HANDLE_VALUE;
        return 0;
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
    } else if (find_text(line, "\"hello\"")) {
        char hello[160];
        wsprintfA(hello, "{\"op\":\"ready\",\"pid\":%u,\"build\":\"%s\"}\n",
                  GetCurrentProcessId(), UMBRA_CLIENT_VERSION);
        pipe_line(hello);
    } else if (find_text(line, "\"bye\"")) {
        push_command(OP_SHUTDOWN, 0, "");
    }
}

static void serve(void)
{
    /* GUI <-> core: one JSON object per line. Never blocks the client: the pipe is NOWAIT and
       an idle pass only sleeps. */
    char chunk[4096];
    DWORD read = 0;

    while (g.pipe != INVALID_HANDLE_VALUE) {
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
            break;
        } else {
            /* idle: the runner fills the bus, we only forward it, so a short sleep costs
               nothing on the client's frame budget */
            Sleep(12);
        }
        drain_output();
        /* nothing else to do with the bus: the runner drains it and emits straight out */
    }
    DisconnectNamedPipe(g.pipe);
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
    if (!open_pipe()) emit("e", 0, "pipe: no gui could connect");

    stage("scan");
    if (!resolve()) {
        emit("e", 0, "resolve failed, see the scan stage");
    }

    stage("state");
    g.state = state_from_scriptcontext();
    if (!g.state) {
        fail("state", "lua_State not found yet");
    } else {
        char *apiChunk = read_chunk("api.luau");
        char *chunk = read_chunk("runner.luau");
        if (!chunk) fail("compile", "runner.luau missing from %LOCALAPPDATA%\\Umbra");
        else {
            emit("i", 0, "state found, spawning runner");
            spawn_runner(g.state, apiChunk, chunk);
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
