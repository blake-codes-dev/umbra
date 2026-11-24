using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using Umbra.Services;

namespace Umbra;

public partial class MainWindow : Window
{
    private readonly IBackend _backend = new MockBackend();

    public MainWindow()
    {
        InitializeComponent();
        _backend.StateChanged += OnState;
        _backend.Output += OnOutput;
        Loaded += (_, _) =>
        {
            PidBox.Text = GuessPid();
            OnOutput(new("ready. mock backend, no client needed.", false));
        };
    }

    private static string GuessPid()
    {
        var procs = System.Diagnostics.Process.GetProcessesByName("RobloxPlayerBeta");
        return procs.Length > 0 ? procs[0].Id.ToString() : "";
    }

    private void TitleBar_Drag(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount == 2)
        {
            WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
            return;
        }
        if (e.ButtonState == MouseButtonState.Pressed) DragMove();
    }

    private void Min_Click(object sender, RoutedEventArgs e) => WindowState = WindowState.Minimized;
    private void Max_Click(object sender, RoutedEventArgs e) =>
        WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
    private void Close_Click(object sender, RoutedEventArgs e) => Close();

    private void Nav_Checked(object sender, RoutedEventArgs e)
    {
        if (PageHome is null) return;
        var tag = (sender as RadioButton)?.Tag as string ?? "home";
        PageHome.Visibility = tag == "home" ? Visibility.Visible : Visibility.Collapsed;
        PageScripts.Visibility = tag == "scripts" ? Visibility.Visible : Visibility.Collapsed;
        PageConsole.Visibility = tag == "console" ? Visibility.Visible : Visibility.Collapsed;
        PageSettings.Visibility = tag == "settings" ? Visibility.Visible : Visibility.Collapsed;
    }

    private async void Attach_Click(object sender, RoutedEventArgs e)
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

    private void Detach_Click(object sender, RoutedEventArgs e)
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
            FontSize = 12.5,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 1, 0, 1),
            Foreground = line.IsError
                ? (Brush)FindResource("BadBrush")
                : line.IsEcho ? (Brush)FindResource("AccentBrush") : (Brush)FindResource("DimBrush"),
        };
        ConsoleHost.Children.Add(block);
        ConsoleScroll.ScrollToEnd();
    }

    private void Run_Click(object sender, RoutedEventArgs e) => Run();
    private void ClearEditor_Click(object sender, RoutedEventArgs e) => Editor.Clear();
    private void ClearConsole_Click(object sender, RoutedEventArgs e) => ConsoleHost.Children.Clear();

    private void Editor_KeyDown(object sender, KeyEventArgs e)
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
