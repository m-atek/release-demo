using System.Net.Http;
using System.Reflection;
using System.Windows;
using System.Windows.Threading;
using Microsoft.Win32;
using StoreAssistant.Core;

namespace StoreAssistant;

public partial class MainWindow : Window
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(15) };
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(30) };

    public MainWindow()
    {
        InitializeComponent();
        VersionText.Text = Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "dev";
        _timer.Tick += async (_, _) => await RefreshAsync();
        Loaded += async (_, _) => { await RefreshAsync(); _timer.Start(); };
    }

    private async Task RefreshAsync()
    {
        var setting = Registry.GetValue(@"HKEY_LOCAL_MACHINE\SOFTWARE\Expeditious Software\Store Assistant", "OrdersUrl", null) as string;
        var source = OrdersSource.FromSetting(Environment.GetEnvironmentVariable("STORE_ASSISTANT_ORDERS_URL") ?? setting);
        if (source is null)
        {
            SummaryText.Text = "No orders source set (OrdersUrl).";
            return;
        }
        try
        {
            var day = OrdersDay.Parse(await Http.GetStringAsync(source));
            SummaryText.Text = $"{day.Count} orders today";
            OrdersList.ItemsSource = day.Orders;
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException or System.Text.Json.JsonException)
        {
            SummaryText.Text = $"Couldn't load today's orders: {error.Message}";
        }
    }
}
