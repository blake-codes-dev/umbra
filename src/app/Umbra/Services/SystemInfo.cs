using System;
using System.Collections.Generic;
using System.Linq;
using System.Management;
using System.Security.Principal;

namespace Umbra.Services;

/// <summary>
/// The device identity a Roblox account is tied to, plus the half that is safe to change.
/// Everything here was measured on the target machine before it was written down.
/// </summary>
public static class SystemInfo
{
    public static bool IsAdmin()
    {
        try
        {
            using var identity = WindowsIdentity.GetCurrent();
            var principal = new WindowsPrincipal(identity);
            return principal.IsInRole(WindowsBuiltInRole.Administrator);
        }
        catch (Exception)
        {
            return false;
        }
    }

    public static string MachineGuid() => ReadHklm(@"SOFTWARE\Microsoft\Cryptography", "MachineGuid");
    public static string ProductId() => ReadHklm(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "ProductId");
    public static string InstallDate() => ReadHklm(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "InstallDate");
    public static string ComputerName() => Environment.MachineName;
    public static string VolumeSerial() => Wmi("Win32_LogicalDisk WHERE DeviceID='C:'", "VolumeSerialNumber");
    public static string SystemUuid() => Wmi("Win32_ComputerSystemProduct", "UUID");
    public static string BaseboardSerial() => Wmi("Win32_BaseBoard", "SerialNumber");
    public static string BiosSerial() => Wmi("Win32_BIOS", "SerialNumber");
    public static string CpuId() => Wmi("Win32_Processor", "ProcessorId");
    public static string DiskSerial() => Wmi("Win32_DiskDrive WHERE Index=0", "SerialNumber");

    public static List<string> PhysicalMacs()
    {
        var found = new List<string>();
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT MACAddress FROM Win32_NetworkAdapter WHERE PhysicalAdapter = True AND MACAddress IS NOT NULL");
            foreach (ManagementObject item in searcher.Get())
            {
                var mac = item["MACAddress"]?.ToString()?.Replace(":", "");
                if (mac is { Length: 12 }) found.Add(mac);
            }
        }
        catch (Exception)
        {
        }
        return found;
    }

    /// <summary>Writes a machine value that needs elevation. Returns false without admin.</summary>
    public static bool SetComputerName(string name)
    {
        if (string.IsNullOrWhiteSpace(name) || name.Length > 15) return false;
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT * FROM Win32_ComputerSystem");
            foreach (ManagementObject item in searcher.Get())
            {
                using var inParams = item.GetMethodParameters("Rename");
                inParams["Name"] = name;
                item.InvokeMethod("Rename", inParams, null);
            }
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static string ReadHklm(string path, string name)
    {
        try
        {
            using var key = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(path);
            return key?.GetValue(name)?.ToString() ?? "";
        }
        catch (Exception)
        {
            return "";
        }
    }

    private static string Wmi(string query, string field)
    {
        try
        {
            using var searcher = new ManagementObjectSearcher("SELECT " + field + " FROM " + query);
            return searcher.Get().Cast<ManagementObject>()
                .Select(o => o[field]?.ToString() ?? "")
                .FirstOrDefault(v => v.Length > 0) ?? "";
        }
        catch (Exception)
        {
            return "";
        }
    }
}
