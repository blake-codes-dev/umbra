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

/* the module list comes from the PEB in module_base(); no psapi, no exports */

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

/* source -> bytecode -> closure. Returns the same codes as luau_load so callers stay simple. */
static int load_source(lua_State *state, const char *chunkname, const char *source)
{
    size_t size = 0;
    char *bytecode;
    int status;
    if (!g.compile) return -1;
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
    if (!state || !g.load || !g.compile || !g.pcall || !g.createtable) {
        fail("resume", "no state or no luau surface");
        return;
    }
    if (!chunk) {
        fail("compile", "runner.luau missing");
        return;
    }
    UMBRA_TRY {
        push_bridge(state, apiChunk);
        if (load_source(state, "@Umbra/runner", chunk) != 0) {
            fail("compile", g.tolstring ? g.tolstring(state, -1, 0) : "runner did not compile");
            return;
        }
        push_bus(state);
        if (g.pcall(state, 2, 0, 0) != 0)
            fail("resume", g.tolstring ? g.tolstring(state, -1, 0) : "runner did not start");
    }
    UMBRA_EXCEPT(EXCEPTION_EXECUTE_HANDLER) {
        fail("resume", "structured exception while starting the runner");
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

static int bridge_write(lua_State *state, int append)
{
    char path[MAX_PATH];
    char text[8192];
    HANDLE file;
    DWORD written = 0;
    if (!argstr(state, 1, path, MAX_PATH) || !resolve_path(path)) return failarg(state, "bad path"), 2;
    if (!argstr(state, 2, text, sizeof(text))) return failarg(state, "bad body"), 2;
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
    char partial[MAX_PATH];
    if (!argstr(state, 1, path, MAX_PATH) || !resolve_path(path)) return failarg(state, "bad path"), 2;
    lstrcpyA(partial, path);
    for (char *c = partial; *c; c++) {
        if (*c != '\\') continue;
        *c = 0;
        if (lstrlenA(partial) > 3) CreateDirectoryA(partial, 0);
        *c = '\\';
    }
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
    if (apiSource) {
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
