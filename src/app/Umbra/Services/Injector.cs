using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace Umbra.Services;

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
    private const uint ProcessAccess = 0x1F0FFF;
    private const uint MemCommitReserve = 0x3000;
    private const uint PageExecuteReadWrite = 0x40;
    private const uint PageReadWrite = 0x04;
    private const uint PageExecuteRead = 0x20;
    private const uint MemRelease = 0x8000;
    private const uint MemPrivate = 0x20000;

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

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool VirtualFreeEx(IntPtr process, IntPtr at, UIntPtr size, uint type);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool VirtualProtectEx(IntPtr process, IntPtr at, UIntPtr size, uint protect, out uint old);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool WriteProcessMemory(IntPtr process, IntPtr at, byte[] buffer, UIntPtr size, out UIntPtr written);

    [DllImport("kernel32", SetLastError = true)]
    private static extern bool ReadProcessMemory(IntPtr process, IntPtr at, byte[] buffer, UIntPtr size, out UIntPtr read);

    [DllImport("kernel32", SetLastError = true)]
    private static extern IntPtr CreateRemoteThread(IntPtr process, IntPtr attributes, UIntPtr stack,
        IntPtr start, IntPtr parameter, uint flags, out uint id);

    [DllImport("kernel32", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Ansi)]
    private static extern IntPtr GetModuleHandleA(string name);

    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Ansi)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);

    public sealed record Result(bool Ok, string Message, int Pid);

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

        var process = OpenProcess(ProcessAccess, false, pid);
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
                MemCommitReserve, PageReadWrite);
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

                // the core's entry does the rest; it never returns (it serves the pipe)
                var start = remote + (nint)pe.EntryPoint;
                Protect(process, remote, pe.SizeOfImage, PageExecuteReadWrite);
                var thread = CreateRemoteThread(process, IntPtr.Zero, UIntPtr.Zero, start, IntPtr.Zero, 0, out _);
                if (thread == IntPtr.Zero) return new(false, "remote thread refused", pid);
                WaitForSingleObject(thread, 2000);
                CloseHandle(thread);

                // give the core a moment to create its pipe, then let the caller attach
                for (var i = 0; i < 40; i++)
                {
                    if (Directory.GetFiles(@"\\.\pipe\", $"umbra-{pid}").Length > 0) break;
                    Thread.Sleep(50);
                }
                return new(true, $"mapped {pe.SizeOfImage / 1024} KB", pid);
            }
            catch (Exception)
            {
                VirtualFreeEx(process, remote, UIntPtr.Zero, MemRelease);
                throw;
            }
        }
        catch (Exception ex)
        {
            return new(false, ex.GetType().Name + ": " + ex.Message, pid);
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

    /// <summary>The parts of the PE format the mapper needs, and nothing else.</summary>
    private sealed class PeImage
    {
        public ushort Machine;
        public uint SizeOfImage;
        public uint SizeOfHeaders;
        public uint EntryPoint;
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
                Machine = BitConverter.ToUInt16(image, coff),
            };

            var optional = coff + 20;
            var magic = BitConverter.ToUInt16(image, optional);
            if (magic != 0x20B) return null;                     // PE32+ only
            parsed.EntryPoint = BitConverter.ToUInt32(image, optional + 16);
            parsed.SizeOfImage = BitConverter.ToUInt32(image, optional + 56);
            parsed.SizeOfHeaders = BitConverter.ToUInt32(image, optional + 60);

            var directories = optional + 112;
            parsed.RelocRva = BitConverter.ToUInt32(image, directories + 5 * 8);
            parsed.RelocSize = BitConverter.ToUInt32(image, directories + 5 * 8 + 4);
            parsed.ImportRva = BitConverter.ToUInt32(image, directories + 1 * 8);

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

        /// <summary>Adds the difference between where the image wants to be and where it is.</summary>
        public bool ApplyRelocations(IntPtr process, IntPtr remote, byte[] image)
        {
            if (RelocSize == 0) return true;
            long delta = remote.ToInt64() - (long)0x140000000;
            if (delta == 0) return true;

            var block = new byte[RelocSize];
            Array.Copy(image, RelocRva, block, 0, (int)Math.Min(RelocSize, image.Length - RelocRva));

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
                if (local == IntPtr.Zero) return false;          // the target must have it loaded

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
                        address = GetProcAddress(local, "#" + (value & 0xFFFF));
                    }
                    else
                    {
                        // the hint/name record lives in the remote image, not ours
                        var rva = (uint)(value & 0x7FFFFFFF);
                        var hint = new byte[2];
                        if (!ReadProcessMemory(process, remote + (int)rva, hint, (UIntPtr)2, out _)) return false;
                        var name = ReadString(process, remote + (int)rva + 2, 128);
                        if (string.IsNullOrEmpty(name)) return false;
                        address = GetProcAddress(local, name);
                        if (address == IntPtr.Zero) return false;
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
