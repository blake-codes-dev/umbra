using System;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using Lyphen.Services;

namespace Lyphen;

public partial class SpooferWindow : Window
{
    private Identity _wanted = Identity.Read();
    private readonly bool _admin = SystemInfo.IsAdmin();

    public SpooferWindow()
    {
        InitializeComponent();
        Banner.Visibility = _admin ? Visibility.Collapsed : Visibility.Visible;
        BuildRows();
        BuildMacs();
        foreach (var country in LocationTable.All().Values) CountryPick.Items.Add(country);
        StatusText.Text = _admin ? "administrator: writes will land" : "read only until elevated";
        Loaded += (_, _) => Animate(PageIdentity);
    }

    public void TitleBar_Drag(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount == 2)
        {
            WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
            return;
        }
        if (e.ButtonState == MouseButtonState.Pressed) DragMove();
    }

    public void Min_Click(object sender, RoutedEventArgs e) => WindowState = WindowState.Minimized;
    public void Max_Click(object sender, RoutedEventArgs e) =>
        WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
    public void Close_Click(object sender, RoutedEventArgs e) => Close();

    public void Nav_Checked(object sender, RoutedEventArgs e)
    {
        if (PageIdentity is null) return;
        var tag = (sender as RadioButton)?.Tag as string ?? "identity";
        PageIdentity.Visibility = tag == "identity" ? Visibility.Visible : Visibility.Collapsed;
        PageNetwork.Visibility = tag == "network" ? Visibility.Visible : Visibility.Collapsed;
        PageAccount.Visibility = tag == "account" ? Visibility.Visible : Visibility.Collapsed;
        PageProfiles.Visibility = tag == "profiles" ? Visibility.Visible : Visibility.Collapsed;
        var page = tag switch
        {
            "network" => PageNetwork,
            "account" => PageAccount,
            "profiles" => PageProfiles,
            _ => PageIdentity,
        };
        Animate(page);
    }

    /// <summary>Fade and rise, the same entrance the executor uses. Never a snap.</summary>
    public static void Animate(UIElement page)
    {
        var transform = new TranslateTransform(0, 10);
        page.RenderTransform = transform;
        var ease = new CubicEase { EasingMode = EasingMode.EaseOut };
        page.BeginAnimation(OpacityProperty,
            new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(180)) { EasingFunction = ease });
        transform.BeginAnimation(TranslateTransform.YProperty,
            new DoubleAnimation(10, 0, TimeSpan.FromMilliseconds(180)) { EasingFunction = ease });
    }

    private void BuildRows()
    {
        RowsHost.Children.Clear();
        AddRow("machine guid", () => _wanted.MachineGuid, v => _wanted.MachineGuid = v, Spoofer.Guid);
        AddRow("product id", () => _wanted.ProductId, v => _wanted.ProductId = v, Spoofer.ProductId);
        AddRow("computer name", () => _wanted.ComputerName, v => _wanted.ComputerName = v, Spoofer.ComputerName);
        AddRow("mac addresses", () => string.Join("  ", _wanted.Macs), v => { }, () => Spoofer.Mac());
        AddReadOnly("volume serial", () => SystemInfo.VolumeSerial());
        AddReadOnly("system uuid", () => SystemInfo.SystemUuid());
        AddReadOnly("baseboard serial", () => SystemInfo.BaseboardSerial());
        AddReadOnly("bios serial", () => SystemInfo.BiosSerial());
        AddReadOnly("cpu id", () => SystemInfo.CpuId());
        AddReadOnly("disk serial", () => SystemInfo.DiskSerial());
    }

    private void AddRow(string labelText, Func<string> read, Action<string> write, Func<string> generate)
    {
        var valueText = new TextBlock
        {
            Style = (Style)FindResource("Mono"),
            TextTrimming = TextTrimming.CharacterEllipsis,
        };
        void Refresh() => valueText.Text = read();

        var card = new Border
        {
            Style = (Style)FindResource("Card"),
            Padding = new Thickness(14, 10, 14, 10),
            Margin = new Thickness(0, 0, 0, 8),
        };
        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(150) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var name = new TextBlock
        {
            Text = labelText,
            FontSize = 11.5,
            Foreground = (Brush)FindResource("FaintBrush"),
            VerticalAlignment = VerticalAlignment.Center,
        };
        Grid.SetColumn(name, 0);
        Grid.SetColumn(valueText, 1);
        valueText.VerticalAlignment = VerticalAlignment.Center;

        var buttons = new StackPanel { Orientation = Orientation.Horizontal };
        var copy = new Button
        {
            Style = (Style)FindResource("BtnQuiet"),
            Content = "copy",
            Padding = new Thickness(8, 4, 8, 4),
            FontSize = 11,
        };
        copy.Click += (_, _) =>
        {
            try
            {
                Clipboard.SetText(read());
                StatusText.Text = labelText + " copied";
            }
            catch (Exception)
            {
            }
        };
        var regenerate = new Button
        {
            Style = (Style)FindResource("Btn"),
            Content = "new",
            Padding = new Thickness(10, 4, 10, 4),
            FontSize = 11,
            Margin = new Thickness(6, 0, 0, 0),
        };
        regenerate.Click += (_, _) =>
        {
            write(generate());
            Refresh();
            StatusText.Text = labelText + " regenerated, press apply to write it";
        };
        buttons.Children.Add(copy);
        buttons.Children.Add(regenerate);
        Grid.SetColumn(buttons, 2);

        grid.Children.Add(name);
        grid.Children.Add(valueText);
        grid.Children.Add(buttons);
        card.Child = grid;
        RowsHost.Children.Add(card);
        Refresh();
    }

    private void AddReadOnly(string labelText, Func<string> read)
    {
        var valueText = new TextBlock
        {
            Style = (Style)FindResource("Mono"),
            Text = read() is { Length: > 0 } v ? v : "not readable",
            TextTrimming = TextTrimming.CharacterEllipsis,
            VerticalAlignment = VerticalAlignment.Center,
        };
        var card = new Border
        {
            Style = (Style)FindResource("CardFlat"),
            Padding = new Thickness(14, 10, 14, 10),
            Margin = new Thickness(0, 0, 0, 8),
        };
        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(150) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        var name = new TextBlock
        {
            Text = labelText,
            FontSize = 11.5,
            Foreground = (Brush)FindResource("FaintBrush"),
            VerticalAlignment = VerticalAlignment.Center,
        };
        grid.Children.Add(name);
        Grid.SetColumn(valueText, 1);
        grid.Children.Add(valueText);
        card.Child = grid;
        RowsHost.Children.Add(card);
    }

    private void BuildMacs()
    {
        MacHost.Children.Clear();
        if (_wanted.Macs.Count == 0)
        {
            MacHost.Children.Add(new TextBlock
            {
                Text = "no physical adapters found",
                Style = (Style)FindResource("Muted"),
            });
            return;
        }
        foreach (var mac in _wanted.Macs)
        {
            MacHost.Children.Add(new TextBlock
            {
                Text = mac,
                Style = (Style)FindResource("Mono"),
                Margin = new Thickness(0, 0, 0, 6),
            });
        }
    }

    public void NewMacs_Click(object sender, RoutedEventArgs e)
    {
        _wanted.Macs = _wanted.Macs.ConvertAll(_ => Spoofer.Mac());
        BuildMacs();
        BuildRows();
        StatusText.Text = "macs regenerated, press apply to write them";
    }

    public void Apply_Click(object sender, RoutedEventArgs e)
    {
        if (!_admin)
        {
            StatusText.Text = "needs administrator, use the banner";
            Elevate_Click(sender, e);
            return;
        }
        var results = Spoofer.Apply(_wanted);
        StatusText.Text = Summarise(results);
    }

    public void Restore_Click(object sender, RoutedEventArgs e)
    {
        if (!_admin)
        {
            StatusText.Text = "needs administrator, use the banner";
            Elevate_Click(sender, e);
            return;
        }
        var results = Spoofer.Restore();
        StatusText.Text = Summarise(results);
        _wanted = Identity.Read();
        BuildRows();
        BuildMacs();
    }

    private static string Summarise(List<(string field, bool ok, string detail)> results)
    {
        var ok = 0;
        foreach (var (_, success, _) in results) if (success) ok++;
        var first = results.FindIndex(r => !r.ok);
        return first >= 0
            ? $"{ok}/{results.Count} written · {results[first].field}: {results[first].detail}"
            : $"{ok}/{results.Count} written";
    }

    public void CopyPrompt_Click(object sender, RoutedEventArgs e)
    {
        Elevation.CopyPrompt();
        StatusText.Text = "command copied, paste it into a terminal";
    }

    public void Elevate_Click(object sender, RoutedEventArgs e)
    {
        if (!Elevation.Relaunch())
        {
            Elevation.CopyPrompt();
            StatusText.Text = "elevation was refused, command copied instead";
        }
    }

    public async void ReadCountry_Click(object sender, RoutedEventArgs e)
    {
        using var web = new RobloxWeb();
        if (!web.LoadCookie(out var where))
        {
            CookieText.Text = "No cookie found. Open the Roblox client and log in, then try again.";
            return;
        }
        CookieText.Text = "cookie: " + where;
        var (ok, country, id, detail) = await web.CurrentAsync();
        if (!ok)
        {
            CountryText.Text = "read failed: " + detail;
            return;
        }
        CountryText.Text = $"{country} · id {id} · {detail}";
        LocationTable.Learn(id, country);
        if (!CountryPick.Items.Contains(country)) CountryPick.Items.Add(country);
    }

    public async void SetCountry_Click(object sender, RoutedEventArgs e)
    {
        var name = CountryPick.SelectedItem as string ?? CountryPick.Text;
        var resolved = LocationTable.Resolve(name);
        if (resolved is null)
        {
            CountryResult.Text = "Pick a country from the list.";
            return;
        }
        CountryResult.Text = "writing…";
        using var web = new RobloxWeb();
        if (!web.LoadCookie(out _))
        {
            CountryResult.Text = "No cookie found. Open the Roblox client and log in first.";
            return;
        }
        var (ok, detail) = await web.SetAsync(resolved.Value.id);
        CountryResult.Text = (ok ? "changed: " : "not changed: ") + detail;
        var after = await web.CurrentAsync();
        if (after.ok) LocationTable.Learn(after.id, after.country);
    }

    public void SaveProfile_Click(object sender, RoutedEventArgs e)
    {
        var name = ProfileName.Text.Trim();
        if (name.Length == 0)
        {
            StatusText.Text = "name the profile first";
            return;
        }
        Spoofer.SaveProfile(name, _wanted);
        BuildProfiles();
        StatusText.Text = "profile saved: " + name;
    }

    private void BuildProfiles()
    {
        ProfileHost.Children.Clear();
        var profiles = Spoofer.Profiles();
        if (profiles.Count == 0)
        {
            ProfileHost.Children.Add(new TextBlock
            {
                Text = "nothing saved yet", Style = (Style)FindResource("Muted"),
            });
            return;
        }
        foreach (var (name, identity) in profiles)
        {
            var card = new Border
            {
                Style = (Style)FindResource("Card"),
                Padding = new Thickness(14, 10, 14, 10),
                Margin = new Thickness(0, 0, 0, 8),
            };
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var text = new StackPanel();
            text.Children.Add(new TextBlock { Text = name, FontWeight = FontWeights.SemiBold });
            text.Children.Add(new TextBlock
            {
                Text = identity.ComputerName + " · " + identity.Macs.Count + " macs",
                Style = (Style)FindResource("Muted"),
            });
            var actions = new StackPanel { Orientation = Orientation.Horizontal };
            var load = new Button
            {
                Style = (Style)FindResource("Btn"), Content = "load",
                Padding = new Thickness(10, 4, 10, 4), FontSize = 11,
            };
            load.Click += (_, _) =>
            {
                _wanted = identity;
                BuildRows();
                BuildMacs();
                NavIdentity.IsChecked = true;
                StatusText.Text = "profile loaded: " + name;
            };
            var remove = new Button
            {
                Style = (Style)FindResource("BtnQuiet"), Content = "delete",
                Padding = new Thickness(8, 4, 8, 4), FontSize = 11, Margin = new Thickness(4, 0, 0, 0),
            };
            remove.Click += (_, _) =>
            {
                Spoofer.DeleteProfile(name);
                BuildProfiles();
            };
            actions.Children.Add(load);
            actions.Children.Add(remove);
            Grid.SetColumn(text, 0);
            Grid.SetColumn(actions, 1);
            grid.Children.Add(text);
            grid.Children.Add(actions);
            card.Child = grid;
            ProfileHost.Children.Add(card);
        }
    }
}
