using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Management;
using System.Text.Json;
using Microsoft.Win32;

namespace Umbra.Services;

public sealed class Identity
{
    public string MachineGuid { get; set; } = "";
    public string ProductId { get; set; } = "";
    public string ComputerName { get; set; } = "";
    public string VolumeSerial { get; set; } = "";
    public string SystemUuid { get; set; } = "";
    public string BaseboardSerial { get; set; } = "";
    public string BiosSerial { get; set; } = "";
    public string CpuId { get; set; } = "";
    public string DiskSerial { get; set; } = "";
    public List<string> Macs { get; set; } = new();

    public static Identity Read() => new()
    {
        MachineGuid = SystemInfo.MachineGuid(),
        ProductId = SystemInfo.ProductId(),
        ComputerName = SystemInfo.ComputerName(),
        VolumeSerial = SystemInfo.VolumeSerial(),
        SystemUuid = SystemInfo.SystemUuid(),
        BaseboardSerial = SystemInfo.BaseboardSerial(),
        BiosSerial = SystemInfo.BiosSerial(),
        CpuId = SystemInfo.CpuId(),
        DiskSerial = SystemInfo.DiskSerial(),
        Macs = SystemInfo.PhysicalMacs(),
    };
}

/// <summary>
/// Device identity spoof. Backup first, then write, then say per field what happened.
/// Only values Roblox actually reads are touched; the rest are shown for context.
/// </summary>
public static class Spoofer
{
    private static string Dir =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Umbra");

    private static string BackupPath => Path.Combine(Dir, "identity-backup.json");
    private static string ProfilePath => Path.Combine(Dir, "profiles.json");

    public static Identity? Backup()
    {
        try
        {
            if (File.Exists(BackupPath))
                return JsonSerializer.Deserialize<Identity>(File.ReadAllText(BackupPath));
        }
        catch (Exception)
        {
        }
        return null;
    }

    private static void SaveBackup(Identity identity)
    {
        Directory.CreateDirectory(Dir);
        if (File.Exists(BackupPath)) return;
        File.WriteAllText(BackupPath, JsonSerializer.Serialize(identity,
            new JsonSerializerOptions { WriteIndented = true }));
    }

    public static List<(string field, bool ok, string detail)> Apply(Identity wanted)
    {
        var results = new List<(string, bool, string)>();
        var original = Identity.Read();
        SaveBackup(original);

        if (!SystemInfo.IsAdmin())
        {
            results.Add(("elevation", false, "not administrator, nothing was written"));
            return results;
        }

        results.Add(("MachineGuid", WriteMachineGuid(wanted.MachineGuid), Advise("machine guid")));
        results.Add(("ProductId", WriteProductId(wanted.ProductId), Advise("product id")));
        results.Add(("ComputerName", SystemInfo.SetComputerName(wanted.ComputerName),
            "applies after a restart"));
        for (var index = 0; index < wanted.Macs.Count; index++)
            results.Add(($"mac{index}", WriteMac(index, wanted.Macs[index]), "adapter disabled/enabled needed"));
        return results;
    }

    public static List<(string field, bool ok, string detail)> Restore()
    {
        var saved = Backup();
        if (saved is null) return new List<(string, bool, string)> { ("backup", false, "no backup on disk") };
        return Apply(saved);
    }

    private static string Advise(string field) => field + " written";

    private static bool WriteMachineGuid(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return false;
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(
                @"SOFTWARE\Microsoft\Cryptography", writable: true);
            key?.SetValue("MachineGuid", value);
            return key is not null;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static bool WriteProductId(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return false;
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(
                @"SOFTWARE\Microsoft\Windows NT\CurrentVersion", writable: true);
            key?.SetValue("ProductId", value);
            return key is not null;
        }
        catch (Exception)
        {
            return false;
        }
    }

    /// <summary>Sets NetworkAddress on the driver key that owns the adapter instance.</summary>
    private static bool WriteMac(int index, string mac)
    {
        var clean = mac.Replace(":", "").Replace("-", "").ToUpperInvariant();
        if (clean.Length != 12) return false;
        try
        {
            var guids = new List<string>();
            using (var searcher = new ManagementObjectSearcher(
                       "SELECT GUID FROM Win32_NetworkAdapter WHERE PhysicalAdapter = True AND GUID IS NOT NULL"))
            {
                foreach (ManagementObject item in searcher.Get())
                {
                    var guid = item["GUID"]?.ToString();
                    if (!string.IsNullOrEmpty(guid)) guids.Add(guid);
                }
            }
            if (index >= guids.Count) return false;
            var wanted = guids[index];

            using var classKey = Registry.LocalMachine.OpenSubKey(
                @"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}");
            if (classKey is null) return false;
            foreach (var name in classKey.GetSubKeyNames())
            {
                if (!int.TryParse(name, out _)) continue;
                using var instance = classKey.OpenSubKey(name, writable: true);
                if (instance?.GetValue("NetCfgInstanceId")?.ToString() != wanted) continue;
                instance.SetValue("NetworkAddress", clean);
                return true;
            }
            return false;
        }
        catch (Exception)
        {
            return false;
        }
    }

    // ---------------------------------------------------------------- profiles
    public static Dictionary<string, Identity> Profiles()
    {
        try
        {
            if (File.Exists(ProfilePath))
                return JsonSerializer.Deserialize<Dictionary<string, Identity>>(File.ReadAllText(ProfilePath))
                       ?? new Dictionary<string, Identity>();
        }
        catch (Exception)
        {
        }
        return new Dictionary<string, Identity>();
    }

    public static void SaveProfile(string name, Identity identity)
    {
        if (string.IsNullOrWhiteSpace(name)) return;
        var all = Profiles();
        all[name.Trim()] = identity;
        Directory.CreateDirectory(Dir);
        File.WriteAllText(ProfilePath, JsonSerializer.Serialize(all,
            new JsonSerializerOptions { WriteIndented = true }));
    }

    public static void DeleteProfile(string name)
    {
        var all = Profiles();
        if (!all.Remove(name)) return;
        Directory.CreateDirectory(Dir);
        File.WriteAllText(ProfilePath, JsonSerializer.Serialize(all,
            new JsonSerializerOptions { WriteIndented = true }));
    }

    // ---------------------------------------------------------------- generation
    private static readonly Random Rng = new();

    public static string Guid() => System.Guid.NewGuid().ToString();
    public static string Mac()
    {
        // locally administered, unicast; keeps the OUI shape without clashing with real vendors
        var bytes = new byte[6];
        Rng.NextBytes(bytes);
        bytes[0] = (byte)((bytes[0] | 0x02) & 0xFE);
        return string.Join("", bytes.Select(b => b.ToString("X2")));
    }

    public static string ProductId() =>
        $"{Rng.Next(10000, 99999)}-{Rng.Next(100, 999)}-{Rng.Next(100000, 999999)}-{Rng.Next(10000, 99999)}";

    public static string ComputerName() => "DESKTOP-" + new string(
        Enumerable.Range(0, 7).Select(_ => "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"[Rng.Next(32)]).ToArray());
}
