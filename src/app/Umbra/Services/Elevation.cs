using System;
using System.Diagnostics;
using System.IO;
using System.Windows;

namespace Umbra.Services;

/// <summary>
/// Elevation. Several identity writes live under HKLM, so the app runs fine without
/// admin and tells the user exactly what to run when a write needs it.
/// </summary>
public static class Elevation
{
    public static string ExePath()
    {
        try
        {
            var path = Environment.ProcessPath;
            if (!string.IsNullOrEmpty(path) && File.Exists(path)) return path;
        }
        catch (Exception)
        {
        }
        return Path.Combine(AppContext.BaseDirectory, "Umbra.exe");
    }

    /// <summary>The exact command a user can paste into an elevated terminal.</summary>
    public static string Prompt(string arguments = "")
    {
        var exe = ExePath();
        var tail = string.IsNullOrWhiteSpace(arguments) ? "" : " " + arguments.Trim();
        return $"Start-Process -FilePath \"{exe}\"{tail} -Verb RunAs";
    }

    public static bool Relaunch(string arguments = "")
    {
        try
        {
            var info = new ProcessStartInfo(ExePath(), arguments)
            {
                UseShellExecute = true,
                Verb = "runas",
            };
            Process.Start(info);
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    public static void CopyPrompt(string arguments = "")
    {
        try
        {
            Clipboard.SetText(Prompt(arguments));
        }
        catch (Exception)
        {
        }
    }
}
