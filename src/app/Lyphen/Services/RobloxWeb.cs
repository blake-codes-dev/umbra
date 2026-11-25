using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;

namespace Lyphen.Services;

/// <summary>
/// Account location. Roblox keeps this server side, so the only honest lever is the
/// same endpoint the client itself calls. Measured 2026-11-24:
///   GET/POST accountsettings.roblox.com/v1/account/settings/account-country
///   GET answers { value: { countryName, countryId, ... } }, POST takes { targetCountryId }
///   both need the .ROBLOSECURITY cookie plus the x-csrf-token handshake.
/// The app never asks for a password, it reuses the cookie the client already stored.
/// </summary>
public sealed partial class RobloxWeb : IDisposable
{
    private const string Base = "https://accountsettings.roblox.com/v1/account/settings/account-country";
    private readonly HttpClient _http;
    private string _cookie = "";
    private string _csrf = "";

    public RobloxWeb()
    {
        var handler = new HttpClientHandler { CookieContainer = new CookieContainer(), UseCookies = true };
        _http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(20) };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("Lyphen/0.1");
    }

    [GeneratedRegex(@"\.ROBLOSECURITY=([A-Za-z0-9_\-\.\|]+)")]
    private static partial Regex CookiePattern();

    /// <summary>Reads the live session cookie out of the client's own storage.</summary>
    public bool LoadCookie(out string where)
    {
        foreach (var path in CookieFiles())
        {
            try
            {
                if (!File.Exists(path)) continue;
                var raw = File.ReadAllText(path)
                    .Replace("\\u003D", "=").Replace("\\u003d", "=").Replace("\\/", "/");
                var match = CookiePattern().Match(raw);
                if (!match.Success) continue;
                _cookie = ".ROBLOSECURITY=" + match.Groups[1].Value;
                _http.DefaultRequestHeaders.Remove("Cookie");
                _http.DefaultRequestHeaders.Add("Cookie", _cookie);
                where = path;
                return true;
            }
            catch (Exception)
            {
            }
        }
        where = "";
        return false;
    }

    private static IEnumerable<string> CookieFiles()
    {
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        yield return Path.Combine(local, "RobloxPCGDK", "LocalStorage", "appStorage.json");
        yield return Path.Combine(local, "RobloxPCGDK", "LocalStorage", "appStorage.json.bak");
        yield return Path.Combine(local, "Roblox", "LocalStorage", "appStorage.json");
    }

    private async Task<bool> HandshakeAsync()
    {
        if (_csrf.Length > 0) return true;
        using var probe = new HttpRequestMessage(HttpMethod.Post, Base);
        probe.Content = new StringContent("{}", System.Text.Encoding.UTF8, "application/json");
        using var response = await _http.SendAsync(probe);
        if (response.Headers.TryGetValues("x-csrf-token", out var values))
        {
            foreach (var value in values)
            {
                _csrf = value;
                break;
            }
        }
        return _csrf.Length > 0;
    }

    public async Task<(bool ok, string country, int id, string detail)> CurrentAsync()
    {
        if (_cookie.Length == 0) return (false, "", 0, "no session cookie, open Roblox and log in first");
        try
        {
            using var response = await _http.GetAsync(Base);
            var body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode) return (false, "", 0, $"http {(int)response.StatusCode}");
            using var doc = JsonDocument.Parse(body);
            var value = doc.RootElement.GetProperty("value");
            var name = value.TryGetProperty("countryName", out var n) ? n.GetString() ?? "" : "";
            var id = value.TryGetProperty("countryId", out var i) ? i.GetInt32() : 0;
            return (true, name, id, "read from account settings");
        }
        catch (Exception exc)
        {
            return (false, "", 0, exc.GetType().Name);
        }
    }

    /// <summary>POSTs the target id, then reads the account back so a silent no-op is visible.</summary>
    public async Task<(bool ok, string detail)> SetAsync(int targetId)
    {
        if (_cookie.Length == 0) return (false, "no session cookie");
        if (!await HandshakeAsync()) return (false, "csrf handshake failed");
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, Base);
            request.Headers.Add("x-csrf-token", _csrf);
            request.Content = new StringContent($"{{\"targetCountryId\":{targetId}}}",
                System.Text.Encoding.UTF8, "application/json");
            using var response = await _http.SendAsync(request);
            var body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode)
                return (false, $"rejected with http {(int)response.StatusCode}: {Trim(body)}");
            var after = await CurrentAsync();
            if (!after.ok) return (true, "accepted, but the read back failed");
            if (after.id != targetId) return (false, $"still {after.country} (id {after.id}), Roblox kept the old value");
            return (true, $"now {after.country}");
        }
        catch (Exception exc)
        {
            return (false, exc.GetType().Name);
        }
    }

    private static string Trim(string text) => text.Length > 160 ? text[..160] : text;

    public void Dispose() => _http.Dispose();
}
