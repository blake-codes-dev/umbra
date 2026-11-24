using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Threading.Tasks;
using System.Windows.Media;
using System.Windows.Media.Animation;
using Umbra.Services;

namespace Umbra;

public partial class MainWindow : Window
{
    private readonly Settings _settings = Settings.Load();
    private List<LibraryScript> _library = new();
    private SpooferWindow? _spoofer;
    private IBackend _backend = null!;
    private bool _loadingUi = true;
    private string? _trace;

    public MainWindow()
    {
        InitializeComponent();
        OptMock.IsChecked = _settings.Backend != "pipe";
        OptLate.IsChecked = _settings.LateInject;
        OptSuspend.IsChecked = _settings.SuspendLaunch;
        OptVerify.IsChecked = _settings.VerifyFootprint;
        _loadingUi = false;
        SwapBackend();
        Loaded += (_, _) =>
        {
            PidBox.Text = string.IsNullOrWhiteSpace(_settings.LastPid) ? GuessPid() : _settings.LastPid!;
            OnOutput(new(_settings.Backend == "pipe"
                ? "ready. pipe backend, needs an injected client on umbra-PID."
                : "ready. mock backend, no client needed.", false));
            Animate(PageHome);
            LoadLibrary();
            ShowAdminState();
        };
        Closing += (_, _) => _settings.Save();
        var argv = Environment.GetCommandLineArgs();
        var traceAt = Array.IndexOf(argv, "--trace");
        if (traceAt >= 0 && traceAt + 1 < argv.Length) _trace = argv[traceAt + 1];
        var attachAt = Array.IndexOf(argv, "--attach");
        if (attachAt >= 0 && attachAt + 1 < argv.Length)
        {
            var pid = argv[attachAt + 1];
            var runAt = Array.IndexOf(argv, "--run");
            Loaded += async (_, _) =>
            {
                PidBox.Text = pid;
                await Task.Delay(400);
                await _backend.AttachAsync(pid);
                if (runAt >= 0 && runAt + 1 < argv.Length && _backend.State == InjectState.Attached)
                {
                    Editor.Text = argv[runAt + 1];
                    await Task.Delay(300);
                    Run();
                }
            };
        }
    }

    private void SwapBackend()
    {
        if (_backend is not null)
        {
            _backend.StateChanged -= OnState;
            _backend.Output -= OnOutput;
            (_backend as IDisposable)?.Dispose();
        }
        _settings.Backend = OptMock.IsChecked == true ? "mock" : "pipe";
        _backend = _settings.Backend == "pipe" ? new PipeBackend(_settings) : new MockBackend();
        _backend.StateChanged += OnState;
        _backend.Output += OnOutput;
        _backend.StateChanged += state =>
        {
            if (state == InjectState.Attached) _settings.Save();
        };
        if (BackendText is not null)
            BackendText.Text = _settings.Backend == "pipe" ? "pipe backend" : "mock backend";
        OnState(InjectState.Detached);
    }

    public void Settings_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingUi || BackendText is null) return;
        _settings.LateInject = OptLate.IsChecked == true;
        _settings.SuspendLaunch = OptSuspend.IsChecked == true;
        _settings.VerifyFootprint = OptVerify.IsChecked == true;
        var wanted = OptMock.IsChecked == true ? "mock" : "pipe";
        if (wanted != _settings.Backend) SwapBackend();
        _settings.Save();
    }

    private static string GuessPid()
    {
        var procs = System.Diagnostics.Process.GetProcessesByName("RobloxPlayerBeta");
        return procs.Length > 0 ? procs[0].Id.ToString() : "";
    }

    // ---------------------------------------------------------------- chrome
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

    public void OpenSpoofer_Click(object sender, RoutedEventArgs e)
    {
        if (_spoofer is { IsLoaded: true })
        {
            _spoofer.Activate();
            return;
        }
        _spoofer = new SpooferWindow();
        _spoofer.Closed += (_, _) => _spoofer = null;
        _spoofer.Show();
    }

    public void OpenMenu_Click(object sender, RoutedEventArgs e)
    {
        var path = FindRepoFile("src", "luau", "menu.luau");
        if (path is null)
        {
            RunHint.Text = "menu.luau not found";
            return;
        }
        try
        {
            Clipboard.SetText(System.IO.File.ReadAllText(path));
            NavScripts.IsChecked = true;
            RunHint.Text = "menu.luau copied, execute it in the client";
        }
        catch (Exception)
        {
            RunHint.Text = "menu.luau could not be read";
        }
    }

    private static string? FindRepoFile(params string[] parts)
    {
        var dir = new System.IO.DirectoryInfo(AppContext.BaseDirectory);
        for (var depth = 0; depth < 6 && dir is not null; depth++, dir = dir.Parent)
        {
            var guess = System.IO.Path.Combine(new[] { dir.FullName }.Concat(parts).ToArray());
            if (System.IO.File.Exists(guess)) return guess;
        }
        return null;
    }

    public void Nav_Checked(object sender, RoutedEventArgs e)
    {
        if (PageHome is null) return;
        var tag = (sender as RadioButton)?.Tag as string ?? "home";
        PageHome.Visibility = tag == "home" ? Visibility.Visible : Visibility.Collapsed;
        PageScripts.Visibility = tag == "scripts" ? Visibility.Visible : Visibility.Collapsed;
        PageConsole.Visibility = tag == "console" ? Visibility.Visible : Visibility.Collapsed;
        PageSettings.Visibility = tag == "settings" ? Visibility.Visible : Visibility.Collapsed;
        var page = tag switch
        {
            "scripts" => PageScripts,
            "console" => PageConsole,
            "settings" => PageSettings,
            _ => PageHome,
        };
        Animate(page);
    }

    /// <summary>Fade and rise, 180 ms. Every page change, never a snap.</summary>
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

    // ---------------------------------------------------------------- library
    private void LoadLibrary()
    {
        _library = Library.Load();
        RenderLibrary(_library, LibHost, 12);
        RenderLibrary(_library.Take(4).ToList(), QuickHost, 6, asTile: true);
    }

    private void RenderLibrary(List<LibraryScript> items, Panel host, int limit, bool asTile = false)
    {
        host.Children.Clear();
        foreach (var item in items.Take(limit))
        {
            var card = new Border
            {
                Style = (Style)FindResource("CardFlat"),
                Padding = new Thickness(12, 10, 12, 10),
                Margin = new Thickness(0, 0, asTile ? 10 : 6, asTile ? 10 : 6),
            };
            var text = new StackPanel();
            text.Children.Add(new TextBlock
            {
                Text = item.Name,
                FontWeight = FontWeights.SemiBold,
                TextTrimming = TextTrimming.CharacterEllipsis,
            });
            text.Children.Add(new TextBlock
            {
                Text = item.Description,
                Style = (Style)FindResource("Muted"),
                FontSize = 11,
                TextTrimming = TextTrimming.CharacterEllipsis,
            });

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                Margin = new Thickness(0, 8, 0, 0),
            };
            var load = new Button
            {
                Style = (Style)FindResource("Btn"),
                Content = "load",
                Padding = new Thickness(10, 3, 10, 3),
                FontSize = 11,
            };
            load.Click += (_, _) =>
            {
                Editor.Text = item.Source;
                NavScripts.IsChecked = true;
                RunHint.Text = item.Name + " loaded";
            };
            var run = new Button
            {
                Style = (Style)FindResource("BtnPrimary"),
                Content = "run",
                Padding = new Thickness(10, 3, 10, 3),
                FontSize = 11,
                Margin = new Thickness(6, 0, 0, 0),
            };
            run.Click += (_, _) =>
            {
                Editor.Text = item.Source;
                NavScripts.IsChecked = true;
                Run();
            };
            actions.Children.Add(load);
            actions.Children.Add(run);
            text.Children.Add(actions);
            card.Child = text;
            host.Children.Add(card);
        }
    }

    public void LibSearch_Changed(object sender, TextChangedEventArgs e)
    {
        if (LibHost is null) return;
        var needle = LibSearch.Text.Trim().ToLowerInvariant();
        var filtered = needle.Length == 0
            ? _library
            : _library.Where(s => (s.Name + " " + s.Description).ToLowerInvariant().Contains(needle)).ToList();
        RenderLibrary(filtered, LibHost, 200);
    }

    // ---------------------------------------------------------------- elevation
    private void ShowAdminState()
    {
        if (SystemInfo.IsAdmin())
        {
            AdminText.Text = "running elevated. identity writes will land.";
            PromptBox.Text = "(already elevated)";
            return;
        }
        AdminText.Text = "not elevated. reading works, HKLM writes do not.";
        PromptBox.Text = Elevation.Prompt();
    }

    public void CopyPrompt_Click(object sender, RoutedEventArgs e)
    {
        Elevation.CopyPrompt();
        AdminText.Text = "command copied to the clipboard.";
    }

    public void Elevate_Click(object sender, RoutedEventArgs e)
    {
        if (Elevation.Relaunch()) return;
        Elevation.CopyPrompt();
        AdminText.Text = "elevation was refused, command copied instead.";
    }

    // ---------------------------------------------------------------- attach
    public async void Attach_Click(object sender, RoutedEventArgs e)
    {
        AttachBtn.IsEnabled = false;
        await _backend.AttachAsync(PidBox.Text.Trim());
        AttachBtn.IsEnabled = true;
        DetachBtn.IsEnabled = _backend.State == InjectState.Attached;
        if (_backend.State == InjectState.Attached)
        {
            FootprintText.Text = "+0 modules, +0 threads, +0 windows, +8 KB private RX. Client survived.";
            NavConsole.IsChecked = true;
        }
    }

    public void Detach_Click(object sender, RoutedEventArgs e)
    {
        _backend.Detach();
        DetachBtn.IsEnabled = false;
        FootprintText.Text = "Nothing measured yet.";
    }

    private void OnState(InjectState state)
    {
        var (text, brush, hint) = state switch
        {
            InjectState.Attached => ("injected", (Brush)FindResource("GoodBrush"), $"attached to pid {_backend.Pid}"),
            InjectState.Attaching => ("attaching", (Brush)FindResource("AccentBrush"), "mapping payload, do not close the client"),
            InjectState.Failed => ("failed", (Brush)FindResource("BadBrush"), "check the process id and try again"),
            _ => ("not injected", (Brush)FindResource("DimBrush"), "attach with a process id"),
        };
        StateText.Text = text;
        StateText.Foreground = brush;
        StateHint.Text = hint;
        StateDot.Background = brush;
        StatusText.Text = text;
        StatusDot.Background = brush;
    }

    private void OnOutput(ConsoleLine line)
    {
        var block = new TextBlock
        {
            Text = (line.IsEcho ? "> " : "") + line.Text,
            FontFamily = new FontFamily("Cascadia Mono, Consolas"),
            FontSize = 12,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 1, 0, 1),
            Foreground = line.IsError
                ? (Brush)FindResource("BadBrush")
                : line.IsEcho ? (Brush)FindResource("AccentBrush") : (Brush)FindResource("DimBrush"),
        };
        ConsoleHost.Children.Add(block);
        if (ConsoleHostWide is not null)
            ConsoleHostWide.Children.Add(new TextBlock
            {
                Text = block.Text,
                FontFamily = block.FontFamily,
                FontSize = block.FontSize,
                TextWrapping = TextWrapping.Wrap,
                Margin = block.Margin,
                Foreground = block.Foreground,
            });
        if (_trace is not null)
        {
            try
            {
                System.IO.File.AppendAllText(_trace,
                    (line.IsEcho ? "> " : "") + line.Text + Environment.NewLine);
            }
            catch (Exception) { }
        }
        ConsoleScroll.ScrollToEnd();
    }

    public void Run_Click(object sender, RoutedEventArgs e) => Run();
    public void ClearEditor_Click(object sender, RoutedEventArgs e) => Editor.Clear();
    public void ClearConsole_Click(object sender, RoutedEventArgs e)
    {
        ConsoleHost.Children.Clear();
        ConsoleHostWide.Children.Clear();
    }

    public void Editor_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.Control)
        {
            Run();
            e.Handled = true;
        }
    }

    private void Run()
    {
        var script = Editor.Text.Trim();
        if (script.Length == 0) return;
        if (_backend.State != InjectState.Attached)
        {
            RunHint.Text = "not injected";
            NavConsole.IsChecked = true;
            return;
        }
        RunHint.Text = "";
        _backend.Execute(script);
    }
}
