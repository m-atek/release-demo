using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace StoreAssistant.Core;

/// <summary>One order as orders-service's /api/orders returns it.</summary>
public sealed record Order(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("store")] string Store,
    [property: JsonPropertyName("items")] IReadOnlyList<string> Items,
    [property: JsonPropertyName("total_eur")] decimal TotalEur,
    [property: JsonPropertyName("status")] string Status)
{
    public string Total => TotalEur.ToString("C", CultureInfo.GetCultureInfo("nl-NL"));
}

/// <summary>Today's orders. Count is every order the API sent; Orders the ones this version could read.</summary>
public sealed record OrdersDay(string Date, IReadOnlyList<Order> Orders, int Count)
{
    public static OrdersDay Parse(string json)
    {
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;
        var orders = new List<Order>();
        var count = 0;
        foreach (var element in root.GetProperty("orders").EnumerateArray())
        {
            count++;
            try
            {
                var order = element.Deserialize<Order>();
                if (order is { Id: not null, Store: not null, Items: not null, Status: not null })
                {
                    orders.Add(order);
                }
            }
            catch (JsonException)
            {
                // A row this version can't read is skipped, so one odd order doesn't empty the list.
            }
        }
        return new OrdersDay(root.GetProperty("date").GetString() ?? "", orders, count);
    }
}

/// <summary>Where the app reads its settings: HKLM\SOFTWARE\Expeditious Software\Store Assistant, OrdersUrl.</summary>
public static class OrdersSource
{
    public static Uri? FromSetting(string? value)
    {
        if (string.IsNullOrWhiteSpace(value) || !Uri.TryCreate(value, UriKind.Absolute, out var uri))
        {
            return null;
        }
        var path = uri.AbsolutePath.TrimEnd('/');
        return path.EndsWith("/api/orders", StringComparison.Ordinal) ? uri : new UriBuilder(uri) { Path = path + "/api/orders" }.Uri;
    }
}
