#include <windows.h>

/*  Umbra stub: proves (1) code executes inside a foreign process and
    (2) the process can talk back out. Two channels are tried, both recorded
    in the marker file so a failure is diagnosable rather than silent.  */

static HANDLE g_pipe = INVALID_HANDLE_VALUE;
char g_dbg[256];   /* first variable in .data: the host reads it back with ReadProcessMemory */

static char *put_hex(char *o, unsigned long long v)
{
    static const char *digits = "0123456789abcdef";
    int i;
    *o++ = '0';
    *o++ = 'x';
    for (i = 60; i >= 0; i -= 4)
        *o++ = digits[(v >> i) & 0xF];
    *o = 0;
    return o;
}

static void marker(const char *text)
{
    HANDLE f = CreateFileA(
        "C:/Users/blake/.deepai-code/scratch/2026-11-23-can-you-build-a-loader-4f37ff4c/state/inject/attach.marker",
        GENERIC_WRITE, 0, 0, CREATE_ALWAYS, 0, 0);
    DWORD w = 0;
    if (f != INVALID_HANDLE_VALUE) {
        WriteFile(f, text, lstrlenA(text), &w, 0);
        CloseHandle(f);
    }
}

static void probe(char *out)
{
    char *o = out;
    const char *bus =
        "C:/Users/blake/.deepai-code/scratch/2026-11-23-can-you-build-a-loader-4f37ff4c/state/inject/bus.bin";
    HANDLE f = CreateFileA(bus, GENERIC_READ | GENERIC_WRITE,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, 0, OPEN_EXISTING, 0, 0);
    DWORD e1 = GetLastError();
    HANDLE m = NULL;
    char *p = NULL;
    if (f != INVALID_HANDLE_VALUE) {
        m = CreateFileMappingA(f, 0, PAGE_READWRITE, 0, 4096, 0);
        if (m != NULL)
            p = (char *)MapViewOfFile(m, FILE_MAP_WRITE | FILE_MAP_READ, 0, 0, 4096);
    }
    DWORD e2 = GetLastError();
    o = put_hex(o, (unsigned long long)(ULONG_PTR)f);
    *o++ = ' ';
    o = put_hex(o, e1);
    *o++ = ' ';
    o = put_hex(o, (unsigned long long)(ULONG_PTR)p);
    *o++ = ' ';
    o = put_hex(o, e2);
    *o++ = ' ';
    if (p != NULL) {
        p[0] = 'U'; p[1] = 'M'; p[2] = 'B'; p[3] = 'R';
        lstrcpynA(p + 8, "attached", 120);
        FlushViewOfFile(p, 4096);
        *o++ = 'w';
        UnmapViewOfFile(p);
    }
    if (m != NULL)
        CloseHandle(m);
    if (f != INVALID_HANDLE_VALUE)
        CloseHandle(f);

HANDLE pipe = CreateFileA("\\\\.\\pipe\\umbra_stub", GENERIC_WRITE, 0, 0, OPEN_EXISTING, 0, 0);
    DWORD e3 = GetLastError();
    g_pipe = pipe;
    o = put_hex(o, (unsigned long long)(ULONG_PTR)pipe);
    *o++ = ' ';
    put_hex(o, e3);
}

BOOL WINAPI DllMain(HINSTANCE self, DWORD reason, LPVOID reserved)
{
    (void)self;
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        char buf[220];
        g_dbg[0] = 'E'; g_dbg[1] = 'N'; g_dbg[2] = 'T'; g_dbg[3] = 'E'; g_dbg[4] = 'R'; g_dbg[5] = 0;
        marker("enter");
        buf[0] = 0;
        probe(buf);
        marker(buf);
        if (g_pipe != INVALID_HANDLE_VALUE) {
            DWORD written = 0;
            WriteFile(g_pipe, "stub: attached", 13, &written, 0);
        }
    }
    return TRUE;
}
