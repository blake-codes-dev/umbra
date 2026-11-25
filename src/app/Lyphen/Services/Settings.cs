using System;
using System.IO;
using System.Text.Json;

namespace Lyphen.Services;

public sealed class Settings
{
    public bool LateInject { get; set; } = true;
    public bool SuspendLaunch { get; set; }
    public bool VerifyFootprint { get; set; } = true;
    public bool Elevate { get; set; }
    public string? LastPid { get; set; }
    public string? Backend { get; set; } = "mock";

    private static string Path =>
        System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Lyphen", "settings.json");

    public static Settings Load()
    {
        try
        {
            if (File.Exists(Path))
                return JsonSerializer.Deserialize<Settings>(File.ReadAllText(Path)) ?? new Settings();
        }
        catch (Exception)
        {
            // a broken file must not stop the panel from opening
        }
        return new Settings();
    }

    public void Save()
    {
        try
        {
            var dir = System.IO.Path.GetDirectoryName(Path)!;
            Directory.CreateDirectory(dir);
            File.WriteAllText(Path, JsonSerializer.Serialize(this,
                new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception)
        {
        }
    }
}
