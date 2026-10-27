using StoreAssistant.Core;
using Xunit;

namespace StoreAssistant.Core.Tests;

public class OrdersTests
{
    [Fact]
    public void ReadsEveryOrderFromTheRecordedResponse()
    {
        var day = OrdersDay.Parse(File.ReadAllText("orders-2026-10-27.json"));
        Assert.Equal("2026-10-27", day.Date);
        Assert.Equal(3, day.Count);
        Assert.Equal(day.Count, day.Orders.Count);
        Assert.Equal(14.99m, day.Orders[0].TotalEur);
    }

    [Fact]
    public void SkipsAnOrderItCantReadAndKeepsTheRest()
    {
        const string json = """
            {"date": "2026-10-27", "orders": [
              {"id": "1", "store": "Utrecht Centraal", "items": ["Rain jacket"], "total_eur": 74.5, "status": "packed"},
              {"id": "2", "store": "Utrecht Centraal"}
            ]}
            """;
        var day = OrdersDay.Parse(json);
        Assert.Equal(2, day.Count);
        Assert.Equal(new[] { "1" }, day.Orders.Select(o => o.Id));
    }

    [Theory]
    [InlineData("https://orders.example.invalid/", "https://orders.example.invalid/api/orders")]
    [InlineData("https://orders.example.invalid/api/orders", "https://orders.example.invalid/api/orders")]
    public void OrdersSourceAddsTheApiPath(string setting, string expected)
    {
        Assert.Equal(expected, OrdersSource.FromSetting(setting)?.ToString());
    }

    [Fact]
    public void NoSettingNoSource()
    {
        Assert.Null(OrdersSource.FromSetting(null));
    }
}
