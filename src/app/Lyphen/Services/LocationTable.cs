using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;

namespace Lyphen.Services;

/// <summary>
/// Roblox country ids. The API only ever returns the id of the current country, so the
/// table is a seed plus anything the app has learned: every apply reads the account back
/// and records the id Roblox actually reports, which is what makes a wrong seed self-fix.
/// </summary>
public static class LocationTable
{
    private static string CachePath =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Lyphen", "location-ids.json");

    // seed: common regions. Unverified ids stay honest because every apply is checked.
    private static readonly (int id, string name)[] Seed =
    {
        (1, "United States"), (2, "Canada"), (3, "United Kingdom"), (4, "Australia"),
        (5, "Germany"), (6, "France"), (7, "Netherlands"), (8, "Spain"),
        (9, "Italy"), (10, "Sweden"), (11, "Norway"), (12, "Denmark"),
        (13, "Finland"), (14, "Poland"), (15, "Brazil"), (16, "Mexico"),
        (17, "Argentina"), (18, "Chile"), (19, "Japan"), (20, "South Korea"),
        (21, "Singapore"), (22, "India"), (23, "Turkey"), (24, "South Africa"),
        (25, "Ireland"), (26, "Portugal"), (27, "New Zealand"), (28, "Philippines"),
    };

    public static Dictionary<int, string> All()
    {
        var table = Seed.ToDictionary(p => p.id, p => p.name);
        try
        {
            if (File.Exists(CachePath))
                foreach (var pair in JsonSerializer.Deserialize<Dictionary<string, string>>(
                             File.ReadAllText(CachePath)) ?? new Dictionary<string, string>())
                    if (int.TryParse(pair.Key, out var id) && !string.IsNullOrWhiteSpace(pair.Value))
                        table[id] = pair.Value;
        }
        catch (Exception)
        {
        }
        return table;
    }

    public static void Learn(int id, string name)
    {
        if (id <= 0 || string.IsNullOrWhiteSpace(name)) return;
        var learned = new Dictionary<string, string>();
        try
        {
            if (File.Exists(CachePath))
                learned = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(CachePath))
                           ?? new Dictionary<string, string>();
            learned[id.ToString()] = name;
            Directory.CreateDirectory(Path.GetDirectoryName(CachePath)!);
            File.WriteAllText(CachePath, JsonSerializer.Serialize(learned,
                new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception)
        {
        }
    }

    public static (int id, string name)? Resolve(string name)
    {
        var hit = All().FirstOrDefault(p => string.Equals(p.Value, name, StringComparison.OrdinalIgnoreCase));
        return hit.Value is null ? null : (hit.Key, hit.Value);
    }
}
