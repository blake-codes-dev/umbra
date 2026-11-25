using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace Lyphen.Services;

/// <summary>
/// Manual-maps core.dll into a client process.
///
/// Nothing is registered with the loader: our pages live in a plain VirtualAllocEx region, so
/// there is no module entry, no LDR_DATA_TABLE_ENTRY and no dll path on disk to find. Sections
/// are mapped, base relocations applied and imports bound by reading the target's own export
/// tables. The export is then called once on a remote thread; that thread stays inside
/// <c>serve()</c> for the life of the session and is the only one we add.
///
/// The remote thread is the one detectable cost. Replacing it with a hijack of an existing
/// client thread (context swap, restore on return) is the next step, not a rewrite: only
/// <see cref="Start"/> changes.
/// </summary>
public sealed class Injector
{
    /* Byfron hands out PROCESS_ALL_ACCESS only to elevated callers, so fall back to just the
       rights the map needs: vm read/write/op, thread create, query. */
    private static readonly uint[] AccessMasks =
    {
        0x1F0FFF,                                                   // PROCESS_ALL_ACCESS
        0x0002 | 0x0008 | 0x0010 | 0x0020 | 0x0400 | 0x00100000,    // create thread, vm ops, query
        0x0008 | 0x0010 | 0x0020 | 0x00100000,                      // no thread create
    };
    private const uint MemCommitReserve = 0x3000;
    private const uint PageExecuteReadWrite = 0x40;
    private const uint PageReadWrite = 0x04;
    private const uint PageExecuteRead = 0x20;
    private const uint MemRelease = 0x8000;
    private const uint MemPrivate = 0x20000;
    private const uint ThreadGetContext = 0x8;
    private const uint ThreadSetContext = 0x10;
    private const uint ThreadSuspendResume = 0x2;

    /// <summary>Written into the payload's header padding so a re-inject can find and free it.</summary>
    private static readonly byte[] Marker = { (byte)'U', (byte)'M', (byte)'B', (byte)'R', (byte)'A', (byte)'1' };
    private const int MarkerOffset = 0x100;

    [DllImport("kernel32", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inherit, int pid);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32", SetLastError = true)]
    private static extern IntPtr VirtualAllocEx(IntPtr process, IntPtr at, UIntPtr size, uint type, uint protect);

    [DllImport("kernel32", SetLastError = true)]
    private static extern IntPtr VirtualQueryEx(IntPtr process, IntPtr at, out Region region, UIntPtr size);

    [DllImport("kernel32")]
    private static extern IntPtr GetCurrentProcess();

    [StructLayout(LayoutKind.Sequential)]
    private struct Region
    {
        public IntPtr Base;
        public IntPtr AllocationBase;
        public uint AllocationProtect;
        public IntPtr RegionSize;
        public uint State;
        public uint Protect;
        public uint Type;
    }

    [DllImport("kernel32", CharSet = CharSet.Unicode)]
    private static extern IntPtr LoadLibraryA(string name);

    [DllImport("kernel32", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint pid);

    [DllImport("kernel32")]
    private static extern int GetProcessId(IntPtr process);

    [DllImport("kernel32", CharSet = CharSet.Unicode)]
    private static extern bool Module32First(IntPtr snapshot, ref ModuleEntry entry);

    [DllImport("kernel32", CharSet = CharSet.Unicode)]
    private static extern bool Module32Next(IntPtr snapshot, ref ModuleEntry entry);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ModuleEntry
    {
        public int Size;
        public int ModuleId;
        public int ProcessId;
        public int GlobalUsage;
        public int ProcessUsage;
        public IntPtr Base;
        public int ImageSize;
        public IntPtr Module;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Name;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string Path;
    }

    

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool VirtualProtectEx(IntPtr process, IntPtr at, UIntPtr size, uint protect, out uint old);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool VirtualFreeEx(IntPtr process, IntPtr at, UIntPtr size, uint type);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool WriteProcessMemory(IntPtr process, IntPtr at, byte[] buffer, UIntPtr size, out UIntPtr written);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool ReadProcessMemory(IntPtr process, IntPtr at, byte[] buffer, UIntPtr size, out UIntPtr read);

    [DllImport("kernel32", SetLastError = true)]
    private static extern IntPtr CreateRemoteThread(IntPtr process, IntPtr attributes, UIntPtr stack,
        IntPtr start, IntPtr parameter, uint flags, out uint id);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool GetExitCodeThread(IntPtr thread, out uint code);

    [DllImport("kernel32", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);

    [DllImport("kernel32", SetLastError = true)]
    private static extern uint SuspendThread(IntPtr thread);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool FlushInstructionCache(IntPtr process, IntPtr at, UIntPtr size);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool GetThreadContext(IntPtr thread, IntPtr context);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool SetThreadContext(IntPtr thread, IntPtr context);

    [DllImport("kernel32", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    /// <summary>Queue an APC to a thread, far less visible than a thread of our own.</summary>
    [DllImport("kernel32", SetLastError = true)]
    private static extern bool QueueUserAPC(IntPtr routine, IntPtr thread, IntPtr parameter);

    [DllImport("kernel32", SetLastError = true)]
    private static extern IntPtr OpenThread(uint access, bool inherit, uint id);

    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool WaitNamedPipe(string name, uint timeout);

    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Ansi)]
    private static extern IntPtr GetModuleHandleA(string name);

    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Ansi)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);

    public sealed record Result(bool Ok, string Message, int Pid);

        /// <summary>
        /// Runs the entry on threads the client already has, so no new thread object appears in the
        /// process. Each thread picks the APC up the next time it becomes alertable, which for a
        /// game loop is immediate, so all of them are queued rather than guessing one.
        /// </summary>
    private static int QueueToThreads(IntPtr process, IntPtr start)
    {
        const uint ThreadSetContext = 0x10;      /* required to queue an apc, THREAD_SET_INFORMATION is not */
        const uint ThreadSuspendResume = 0x2;
        var pid = GetProcessId(process);
        var snapshot = CreateToolhelp32Snapshot(0x4, (uint)pid);
        if (snapshot == IntPtr.Zero || snapshot == new IntPtr(-1)) return 0;

        var queued = 0;
        try
        {
            var entry = new ThreadEntry { Size = (uint)Marshal.SizeOf<ThreadEntry>() };
            if (!Thread32First(snapshot, ref entry)) return 0;
            do
            {
                if (entry.OwnerPid != pid) continue;
                var thread = OpenThread(ThreadSetContext | ThreadSuspendResume, false, entry.ThreadId);
                if (thread == IntPtr.Zero) continue;
                if (QueueUserAPC(start, thread, IntPtr.Zero)) queued++;
                CloseHandle(thread);
            } while (Thread32Next(snapshot, ref entry));
        }
        finally { CloseHandle(snapshot); }
        return queued;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ThreadEntry
    {
            public uint Size;
            public uint Usage;
            public uint ThreadId;
            public uint OwnerPid;
            public int BasePriority;
            public int DeltaPriority;
            public uint Flags;
        }

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool Thread32First(IntPtr snapshot, ref ThreadEntry entry);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool Thread32Next(IntPtr snapshot, ref ThreadEntry entry);

    /// <summary>Client processes, newest first. Empty means "launch it for them".</summary>
    public static List<Process> FindClients()
    {
        var found = new List<Process>();
        foreach (var name in new[] { "RobloxPlayerBeta", "Windows10Universal" })
        {
            foreach (var process in Process.GetProcessesByName(name))
            {
                try
                {
                    if (process.MainWindowHandle != IntPtr.Zero || process.WorkingSet64 > 200_000_000)
                        found.Add(process);
                }
                catch (Exception) { }
            }
        }
        return found;
    }

    /// <summary>Injects <paramref name="dllPath"/> into <paramref name="pid"/>.</summary>
    public Result Inject(int pid, string dllPath)
    {
        if (!File.Exists(dllPath)) return new(false, $"no payload at {dllPath}", pid);
        var image = File.ReadAllBytes(dllPath);

        var process = IntPtr.Zero;
        foreach (var mask in AccessMasks)
        {
            process = OpenProcess(mask, false, pid);
            if (process != IntPtr.Zero) break;
        }
        if (process == IntPtr.Zero) return new(false, "open process refused (run as admin?)", pid);
        try
        {
            var pe = PeImage.Parse(image);
            if (pe is null) return new(false, "payload is not a PE", pid);
            if (pe.Machine != 0x8664) return new(false, "payload is not x64", pid);

            // a previous session in the same client would otherwise leak its whole image
            var stale = FindPrevious(process);
            if (stale != IntPtr.Zero) FreePrevious(process, stale);

            var remote = VirtualAllocEx(process, IntPtr.Zero, (UIntPtr)pe.SizeOfImage,
                MemCommitReserve, PageExecuteReadWrite);
            if (remote == IntPtr.Zero) return new(false, "allocation failed", pid);

            try
            {
                // headers first: the image has to be structurally valid before anything runs
                var headers = new byte[pe.SizeOfHeaders];
                Array.Copy(image, headers, (int)Math.Min(pe.SizeOfHeaders, image.Length));
                Array.Copy(Marker, 0, headers, MarkerOffset, Marker.Length);
                if (!Write(process, remote, headers))
                    return new(false, "header write failed", pid);

                foreach (var section in pe.Sections)
                {
                    var data = new byte[section.RawSize];
                    var take = Math.Min(section.RawSize, image.Length - (int)section.RawPointer);
                    if (take > 0) Array.Copy(image, section.RawPointer, data, 0, take);
                    if (section.VirtualSize > 0 && !Write(process, remote + (int)section.VirtualAddress, data))
                        return new(false, $"section {section.Name} write failed", pid);
                }

                if (!pe.ApplyRelocations(process, remote, image))
                    return new(false, "relocation failed", pid);
                if (!pe.BindImports(process, remote))
                    return new(false, "import binding failed", pid);

                /* the COFF entry is DllMain, which a manual map never calls, so the thread starts
                   at the exported lyphen_entry and that does the attach work itself */
                var entryRva = pe.Export("lyphen_entry");
                if (entryRva == 0) entryRva = pe.EntryPoint;
                var start = remote + (nint)entryRva;
                {
                    var cl = PeImage.RemoteModulePublic(process, "RobloxPlayerBeta.exe");
                    var peek = new byte[16];
                    ReadProcessMemory(process, cl + 0x1000, peek, (UIntPtr)16, out _);
                    var peek2 = new byte[16];
                    ReadProcessMemory(process, cl + 0x30000, peek2, (UIntPtr)16, out _);
                    Console.WriteLine($"  client text@1000 {BitConverter.ToString(peek)} text@30000 {BitConverter.ToString(peek2)}");
                }
                if (VirtualQueryEx(process, start, out var region, (UIntPtr)System.Runtime.InteropServices.Marshal.SizeOf<Region>()) != IntPtr.Zero)
                    Console.WriteLine($"  start region base {region.Base.ToInt64():x} size {region.RegionSize.ToInt64():x} state {region.State:x} protect {region.Protect:x}");
                var probe = new byte[16];
                var got = ReadProcessMemory(process, start, probe, (UIntPtr)16, out var nread);
                var wantAt = pe.OffsetOf(entryRva);
                Console.WriteLine($"  k32 local {GetModuleHandleA("kernel32.dll").ToInt64():x} remote {(PeImage.RemoteModulePublic(process, "kernel32.dll")).ToInt64():x}");
                Console.WriteLine($"  entry rva {entryRva:x} at {start.ToInt64():x} read {got}/{nread} bytes {BitConverter.ToString(probe)} want {BitConverter.ToString(image, wantAt, 16)}");
                /* whole image writable+executable first (what hyperion tolerates best), then the
                   sections settle: code executes, data stays writable */
                // left as allocated: executable+writable from the start, nothing to transition
                foreach (var section in pe.Sections)
                {
                    var page = section.Name == ".text" ? PageExecuteRead : PageReadWrite;
                    VirtualProtectEx(process, remote + (int)section.VirtualAddress,
                        (UIntPtr)Math.Max(section.VirtualSize, 0x1000u), page, out _);
                }
                VirtualProtectEx(process, remote, (UIntPtr)pe.SizeOfHeaders, PageExecuteRead, out _);
                foreach (var section in pe.Sections)
                {
                    var at0 = remote + (int)section.VirtualAddress;
                    if (VirtualQueryEx(process, at0, out var q, (UIntPtr)System.Runtime.InteropServices.Marshal.SizeOf<Region>()) != IntPtr.Zero)
                        Console.WriteLine($"  {section.Name} protect {q.Protect:x}");
                    else Console.WriteLine($"  {section.Name} query failed");
                }

                /* client threads and apcs, never a thread of our own: hyperion kills those */
                var queued = Injector.QueueToThreads(process, start);
                var hijacked = Injector.HijackThreads(process, start, 3);
                Console.WriteLine($"  apc {queued} pinned {hijacked}");
                var how = $"mapped {pe.SizeOfImage / 1024} KB";
                var pipe = @"\\.\pipe\lyphen-" + pid;
                var ready = false;
                for (var i = 0; i < 120; i++)
                {
                    if (Directory.GetFiles(@"//./pipe/", $"lyphen-{pid}").Length > 0)
                    {
                        ready = true;
                        break;
                    }
                    Thread.Sleep(50);
                }

                if (!ready && queued == 0 && hijacked == 0)
                {
                    // no thread could be taken: fall back to one of our own, visibly
                    var thread = CreateRemoteThread(process, IntPtr.Zero, UIntPtr.Zero, start, IntPtr.Zero, 0, out _);
                    if (thread == IntPtr.Zero)
                        return new(false, "neither an apc nor a thread took", pid);
                    WaitForSingleObject(thread, 2000);
                    GetExitCodeThread(thread, out var code);
                    Console.WriteLine($"  thread exit {code:x}");
                    CloseHandle(thread);
                    for (var i = 0; i < 40; i++)
                    {
                        if (Directory.GetFiles(@"//./pipe/", $"lyphen-{pid}").Length > 0) break;
                        Thread.Sleep(50);
                    }
                    return new(true, how + ", own thread", pid);
                }

                return new(true, $"{how}, {queued} apc / {hijacked} pinned", pid);
            }
            catch (Exception)
            {
                VirtualFreeEx(process, remote, UIntPtr.Zero, MemRelease);
                throw;
            }
        }
        catch (Exception ex)
        {
            return new(false, ex.GetType().Name + ": " + ex.Message + "\n" + ex.StackTrace, pid);
        }
        finally
        {
            CloseHandle(process);
        }
    }

    private static char[] _unused = Array.Empty<char>();

    /// <summary>Finds an image from an earlier inject by its marker.</summary>
    private static IntPtr FindPrevious(IntPtr process)
    {
        var cursor = (IntPtr)0x10000;
        var info = new Region();
        var size = (UIntPtr)Marshal.SizeOf<Region>();
        while (VirtualQueryEx(process, cursor, out info, size) != IntPtr.Zero)
        {
            var next = info.Base + (int)info.RegionSize;
            if (next <= cursor) break;
            cursor = next;

            if (info.State != 0x1000 || info.Type != MemPrivate) continue;
            if (info.Protect != PageExecuteReadWrite && info.Protect != PageReadWrite) continue;
            if ((long)info.RegionSize < 0x200) continue;

            var probe = new byte[MarkerOffset + Marker.Length];
            if (!ReadProcessMemory(process, info.Base, probe, (UIntPtr)probe.Length, out _)) continue;
            var match = true;
            for (var i = 0; i < Marker.Length; i++)
                if (probe[MarkerOffset + i] != Marker[i]) { match = false; break; }
            if (match) return info.Base;
        }
        return IntPtr.Zero;
    }

    /// <summary>Best effort: a leaked image is worse than a failed free, so failures are silent.</summary>
    private static void FreePrevious(IntPtr process, IntPtr at)
    {
        VirtualFreeEx(process, at, UIntPtr.Zero, MemRelease);
    }

    private static bool Write(IntPtr process, IntPtr at, byte[] data) =>
        WriteProcessMemory(process, at, data, (UIntPtr)data.Length, out var written) &&
        written == (UIntPtr)data.Length;

    private static void Protect(IntPtr process, IntPtr at, uint size, uint page) =>
        VirtualProtectEx(process, at, (UIntPtr)size, page, out _);

    /// <summary>
    /// Runs <paramref name="start"/> on a thread the client already has. Hyperion kills a thread
    /// we create, so instead one client thread is suspended, pointed at a stub that saves every
    /// register, calls us, restores everything and jumps back where it left off.
    /// </summary>
    /* A thread parked in a kernel wait gets its rip restored from the trap frame on return, so a
       single hijack is a coin toss; take a few and let the entry's own guard pick the winner. */
    private static int HijackThreads(IntPtr process, IntPtr start, int want)
    {
        var page = VirtualAllocEx(process, IntPtr.Zero, (UIntPtr)0x1000, MemCommitReserve, PageExecuteReadWrite);
        if (page == IntPtr.Zero) return 0;
        var slotRip = page + 0xF00;
        var slotRsp = page + 0xF08;
        if (!Write(process, page, Stub(start, slotRip, slotRsp))) return 0;
        FlushInstructionCache(process, page, (UIntPtr)0x100);

        var pid = GetProcessId(process);
        var snapshot = CreateToolhelp32Snapshot(0x4, (uint)pid);
        if (snapshot == IntPtr.Zero || snapshot == new IntPtr(-1)) return 0;
        var box = Marshal.AllocHGlobal(0x500);
        var taken = 0;
        try
        {
            var entry = new ThreadEntry { Size = (uint)Marshal.SizeOf<ThreadEntry>() };
            if (!Thread32First(snapshot, ref entry)) return 0;
            var skip = taken % 8;              /* spread picks so a retry lands elsewhere */
            do
            {
                if (taken >= want) break;
                if (entry.OwnerPid != pid) continue;
                var thread = OpenThread(ThreadGetContext | ThreadSetContext | ThreadSuspendResume, false, entry.ThreadId);
                if (thread == IntPtr.Zero) continue;
                try
                {
                    if (SuspendThread(thread) == 0xFFFFFFFF) continue;
                    Marshal.WriteInt32(box, 0x30, 0x10001F);
                    if (!GetThreadContext(thread, box)) { ResumeThread(thread); continue; }
                    if (!Write(process, slotRip, BitConverter.GetBytes(Marshal.ReadInt64(box, 0xF8))))
                    {
                        ResumeThread(thread);
                        continue;
                    }
                    Marshal.WriteInt64(box, 0xF8, page.ToInt64());
                    var set = SetThreadContext(thread, box);
                    ResumeThread(thread);
                    if (set) { taken++; Thread.Sleep(120); }
                }
                finally { CloseHandle(thread); }
            } while (Thread32Next(snapshot, ref entry));
        }
        finally { Marshal.FreeHGlobal(box); CloseHandle(snapshot); }
        return taken;
    }

    private static bool HijackThread(IntPtr process, IntPtr start)
    {
        var pid = GetProcessId(process);
        var snapshot = CreateToolhelp32Snapshot(0x4, (uint)pid);
        if (snapshot == IntPtr.Zero || snapshot == new IntPtr(-1)) return false;

        var page = VirtualAllocEx(process, IntPtr.Zero, (UIntPtr)0x1000, MemCommitReserve, PageExecuteReadWrite);
        if (page == IntPtr.Zero) { CloseHandle(snapshot); return false; }
        var slotRip = page + 0xF00;
        var slotRsp = page + 0xF08;
        if (!Write(process, page, Stub(start, slotRip, slotRsp))) { CloseHandle(snapshot); return false; }
        FlushInstructionCache(process, page, (UIntPtr)0x100);

        var box = Marshal.AllocHGlobal(0x500);
        try
        {
            var entry = new ThreadEntry { Size = (uint)Marshal.SizeOf<ThreadEntry>() };
            if (!Thread32First(snapshot, ref entry)) return false;
            do
            {
                if (entry.OwnerPid != pid) continue;
                var thread = OpenThread(ThreadGetContext | ThreadSetContext | ThreadSuspendResume, false, entry.ThreadId);
                if (thread == IntPtr.Zero) continue;
                try
                {
                    if (SuspendThread(thread) == 0xFFFFFFFF) continue;
                    Marshal.WriteInt32(box, 0x30, 0x10001F);
                    if (!GetThreadContext(thread, box)) { ResumeThread(thread); continue; }
                    if (!Write(process, slotRip, BitConverter.GetBytes(Marshal.ReadInt64(box, 0xF8))))
                    {
                        ResumeThread(thread);
                        continue;
                    }
                    Marshal.WriteInt64(box, 0xF8, page.ToInt64());
                    var set = SetThreadContext(thread, box);
                    ResumeThread(thread);
                    if (set) return true;
                }
                finally { CloseHandle(thread); }
            } while (Thread32Next(snapshot, ref entry));
        }
        finally { Marshal.FreeHGlobal(box); CloseHandle(snapshot); }
        return false;
    }

    /* push everything, call, pop everything, jump back. rsp is saved and restored too, and the
       stack is re-aligned so the call sees a normal frame. */
    private static byte[] Stub(IntPtr entry, IntPtr slotRip, IntPtr slotRsp)
    {
        var code = new List<byte>();
        void Mov(byte lo, byte hi, IntPtr value) { code.Add(lo); code.Add(hi); code.AddRange(BitConverter.GetBytes(value.ToInt64())); }
        Mov(0x49, 0xBB, slotRsp);            // mov r11, slotRsp
        code.AddRange(new byte[] { 0x49, 0x89, 0x23 });   // mov [r11], rsp
        code.Add(0x9C);                                   // pushfq
        foreach (var r in new byte[] { 0x50, 0x51, 0x52, 0x53, 0x55, 0x56, 0x57 })
            code.Add(r);
        code.AddRange(new byte[] { 0x41, 0x50, 0x41, 0x51, 0x41, 0x52, 0x41, 0x53,
                                   0x41, 0x54, 0x41, 0x55, 0x41, 0x56, 0x41, 0x57 });
        code.AddRange(new byte[] { 0x48, 0x83, 0xEC, 0x08 });   // sub rsp, 8
        Mov(0x48, 0xB8, entry);                               // mov rax, entry
        code.AddRange(new byte[] { 0xFF, 0xD0 });             // call rax
        code.AddRange(new byte[] { 0x48, 0x83, 0xC4, 0x08 });   // add rsp, 8
        code.AddRange(new byte[] { 0x41, 0x5F, 0x41, 0x5E, 0x41, 0x5D, 0x41, 0x5C,
                                   0x41, 0x5B, 0x41, 0x5A, 0x41, 0x59, 0x41, 0x58 });
        foreach (var r in new byte[] { 0x5F, 0x5E, 0x5D, 0x5B, 0x5A, 0x59, 0x58 })
            code.Add(r);
        code.Add(0x9D);                                       // popfq
        Mov(0x49, 0xBB, slotRsp);
        code.AddRange(new byte[] { 0x49, 0x8B, 0x23 });   // mov rsp, [r11]
        Mov(0x49, 0xBB, slotRip);
        code.AddRange(new byte[] { 0x41, 0xFF, 0x27 });   // jmp [r11]
        return code.ToArray();
    }

    /// <summary>The parts of the PE format the mapper needs, and nothing else.</summary>
    private sealed class PeImage
    {
        public ushort Machine;
        public uint SizeOfImage;
        public uint SizeOfHeaders;
        public uint EntryPoint;
        public ulong ImageBase;
        public byte[] image;
        public uint ExportRva;
        public uint RelocRva;
        public uint RelocSize;
        public uint ImportRva;
        public List<(string Name, uint VirtualAddress, uint VirtualSize, uint RawPointer, uint RawSize)> Sections { get; } = new();

        public static PeImage? Parse(byte[] image)
        {
            if (image.Length < 0x40 || image[0] != 'M' || image[1] != 'Z') return null;
            var peOffset = BitConverter.ToInt32(image, 0x3C);
            if (peOffset <= 0 || peOffset + 0x18 > image.Length) return null;
            if (BitConverter.ToUInt32(image, peOffset) != 0x00004550) return null;

            var coff = peOffset + 4;
            var parsed = new PeImage
            {
                image = image,
                Machine = BitConverter.ToUInt16(image, coff),
            };

            var optional = coff + 20;
            var magic = BitConverter.ToUInt16(image, optional);
            if (magic != 0x20B) return null;                     // PE32+ only
            parsed.EntryPoint = BitConverter.ToUInt32(image, optional + 16);
            parsed.ImageBase = BitConverter.ToUInt64(image, optional + 24);
            parsed.SizeOfImage = BitConverter.ToUInt32(image, optional + 56);
            parsed.SizeOfHeaders = BitConverter.ToUInt32(image, optional + 60);

            var directories = optional + 112;
            parsed.RelocRva = BitConverter.ToUInt32(image, directories + 5 * 8);
            parsed.RelocSize = BitConverter.ToUInt32(image, directories + 5 * 8 + 4);
            parsed.ImportRva = BitConverter.ToUInt32(image, directories + 1 * 8);
            parsed.ExportRva = BitConverter.ToUInt32(image, directories);

            var sectionCount = BitConverter.ToUInt16(image, coff + 2);
            var at = optional + BitConverter.ToUInt16(image, coff + 16);
            for (var i = 0; i < sectionCount; i++, at += 40)
            {
                if (at + 40 > image.Length) break;
                var name = Encoding.ASCII.GetString(image, at, 8).TrimEnd('\0');
                parsed.Sections.Add((name,
                    BitConverter.ToUInt32(image, at + 12),
                    BitConverter.ToUInt32(image, at + 8),
                    BitConverter.ToUInt32(image, at + 20),
                    Math.Min(BitConverter.ToUInt32(image, at + 16), parsed.SizeOfImage)));
            }
            return parsed;
        }

        /// <summary>RVA to file offset. The byte array is the file, so RVAs cannot index it:
        /// a high section RVA (0xE000 here) is far past the end of a 34 KB file.</summary>
        public int OffsetOf(uint rva)
        {
            foreach (var section in Sections)
            {
                if (rva < section.VirtualAddress) continue;
                var within = rva - section.VirtualAddress;
                if (within >= Math.Max(section.VirtualSize, section.RawSize)) continue;
                return (int)section.RawPointer + (int)within;
            }
            return Sections.Count > 0 ? -1 : (int)rva;
        }

        /// <summary>RVA of an export by name; 0 when the image does not export it.</summary>
        public uint Export(string name)
        {
            if (ExportRva == 0) return 0;
            var at = OffsetOf(ExportRva);
            if (at < 0 || at + 40 > image.Length) return 0;
            var count = Math.Min(BitConverter.ToUInt32(image, at + 24), 64);
            var namesAt = OffsetOf(BitConverter.ToUInt32(image, at + 32));
            var funcsAt = OffsetOf(BitConverter.ToUInt32(image, at + 28));
            var ordinalsAt = OffsetOf(BitConverter.ToUInt32(image, at + 36));
            if (namesAt < 0 || funcsAt < 0 || ordinalsAt < 0) return 0;
            if (namesAt + 4 * count > image.Length || ordinalsAt + 2 * count > image.Length) return 0;
            for (var i = 0; i < count; i++)
            {
                var nameAt = OffsetOf(BitConverter.ToUInt32(image, namesAt + i * 4));
                if (nameAt < 0 || nameAt >= image.Length) continue;
                var end = Array.IndexOf(image, (byte)0, nameAt);
                if (end < 0) continue;
                if (Encoding.ASCII.GetString(image, nameAt, end - nameAt) != name) continue;
                var ordinal = BitConverter.ToUInt16(image, ordinalsAt + i * 2);
                var slot = funcsAt + ordinal * 4;
                return slot + 4 <= image.Length ? BitConverter.ToUInt32(image, slot) : 0;
            }
            return 0;
        }

        /// <summary>Adds the difference between where the image wants to be and where it is.</summary>
        public bool ApplyRelocations(IntPtr process, IntPtr remote, byte[] image)
        {
            if (RelocSize == 0) return true;
            /* the image's own base, not an assumed one: a dll links at 0x180000000, an exe at
               0x140000000, and a wrong delta puts every relocated pointer 1 GB+ into nothing */
            long delta = remote.ToInt64() - (long)(ImageBase != 0 ? ImageBase : 0x140000000);
            if (delta == 0) return true;

            var at0 = OffsetOf(RelocRva);
            if (at0 < 0 || at0 >= image.Length) return true;
            var block = new byte[RelocSize];
            Array.Copy(image, at0, block, 0, (int)Math.Min(RelocSize, image.Length - at0));

            var at = 0;
            while (at + 8 <= block.Length)
            {
                var pageRva = BitConverter.ToUInt32(block, at);
                var size = BitConverter.ToUInt16(block, at + 4);
                if (size < 8) break;
                for (var entry = at + 8; entry + 2 <= at + size; entry += 2)
                {
                    var raw = BitConverter.ToUInt16(block, entry);
                    var type = raw >> 12;
                    if (type == 0) continue;
                    if (type != 10 && type != 3) continue;        // x64 DIR64, and HIGHLOW for safety
                    var target = remote + (int)(pageRva + (raw & 0x0FFF));
                    var bytes = new byte[8];
                    if (!ReadProcessMemory(process, target, bytes, (UIntPtr)8, out _)) return false;
                    var value = BitConverter.ToInt64(bytes, 0) + delta;
                    if (!Write(process, target, BitConverter.GetBytes(value))) return false;
                }
                at += size;
            }
            return true;
        }

        internal static IntPtr RemoteModulePublic(IntPtr process, string name) => RemoteModule(process, name);

        /// <summary>Base of a module in the target, by name. Zero when it is not loaded there.</summary>
        private static IntPtr RemoteModule(IntPtr process, string name)
        {
            var pid = GetProcessId(process);
            var snapshot = CreateToolhelp32Snapshot(0x8, (uint)pid);
            if (snapshot == IntPtr.Zero || snapshot == new IntPtr(-1)) return IntPtr.Zero;
            try
            {
                var entry = new ModuleEntry { Size = Marshal.SizeOf<ModuleEntry>() };
                if (!Module32First(snapshot, ref entry)) return IntPtr.Zero;
                do
                {
                    if (string.Equals(entry.Name, name, StringComparison.OrdinalIgnoreCase))
                        return entry.Base;
                } while (Module32Next(snapshot, ref entry));
                return IntPtr.Zero;
            }
            finally { CloseHandle(snapshot); }
        }

        [DllImport("kernel32", SetLastError = true)]
        private static extern bool VirtualFreeEx(IntPtr process, IntPtr at, UIntPtr size, uint type);

        /// <summary>Binds imports against the module already loaded in the target.</summary>
        public bool BindImports(IntPtr process, IntPtr remote)
        {
            if (ImportRva == 0) return true;
            var offset = 0;
            while (true)
            {
                var buffer = new byte[20];
                if (!ReadProcessMemory(process, remote + (int)(ImportRva + offset), buffer, (UIntPtr)20, out _))
                    return false;
                var originalThunk = BitConverter.ToUInt32(buffer, 0);
                var nameRva = BitConverter.ToUInt32(buffer, 12);
                var firstThunk = BitConverter.ToUInt32(buffer, 16);
                if (originalThunk == 0 && nameRva == 0 && firstThunk == 0) break;
                offset += 20;

                var moduleName = ReadString(process, remote + (int)nameRva, 128);
                if (string.IsNullOrEmpty(moduleName)) return false;

                var local = GetModuleHandleA(moduleName);
                if (local == IntPtr.Zero) local = LoadLibraryA(moduleName);   // winhttp is not ours yet
                if (local == IntPtr.Zero) return false;
                var remoteBase = RemoteModule(process, moduleName);
                if (remoteBase == IntPtr.Zero) return false;      // not present in the target

                var thunkRva = originalThunk != 0 ? originalThunk : firstThunk;
                for (var index = 0; ; index += 8)
                {
                    var thunk = new byte[8];
                    if (!ReadProcessMemory(process, remote + (int)(thunkRva + index), thunk, (UIntPtr)8, out _))
                        return false;
                    var value = BitConverter.ToUInt64(thunk, 0);
                    if (value == 0) break;

                    IntPtr address;
                    if ((value & 0x8000000000000000) != 0)
                    {
                        var ord = GetProcAddress(local, "#" + (value & 0xFFFF));
                        if (ord == IntPtr.Zero) return false;
                        address = remoteBase + (int)(ord.ToInt64() - local.ToInt64());
                    }
                    else
                    {
                        // the hint/name record lives in the remote image, not ours
                        var rva = (uint)(value & 0x7FFFFFFF);
                        var hint = new byte[2];
                        if (!ReadProcessMemory(process, remote + (int)rva, hint, (UIntPtr)2, out _)) return false;
                        var name = ReadString(process, remote + (int)rva + 2, 128);
                        if (string.IsNullOrEmpty(name)) return false;
                        var proc = GetProcAddress(local, name);
                        if (proc == IntPtr.Zero) return false;
                        /* bases differ per process, so port the offset rather than the address */
                        address = remoteBase + (int)(proc.ToInt64() - local.ToInt64());
                    }
                    if (!Write(process, remote + (int)(firstThunk + index), BitConverter.GetBytes(address.ToInt64())))
                        return false;
                }
            }
            return true;
        }

        private static string ReadString(IntPtr process, IntPtr at, int limit)
        {
            var buffer = new byte[limit];
            if (!ReadProcessMemory(process, at, buffer, (UIntPtr)limit, out var read)) return null;
            var length = Array.IndexOf(buffer, (byte)0, 0, (int)read);
            if (length < 0) length = (int)read;
            return Encoding.ASCII.GetString(buffer, 0, length);
        }
    }
}