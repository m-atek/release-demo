using System;
using System.Collections.Generic;
using StoreSync.Core;
using Xunit;

namespace StoreSync.Tests
{
    public class HealthReportTests
    {
        private static readonly DateTime Started = new DateTime(2026, 10, 27, 9, 30, 0, DateTimeKind.Utc);

        private static StoreSettings Settings(string endpoint) => StoreSettings.From(
            endpoint == null
                ? new Dictionary<string, string> { ["StoreId"] = "STORE-004" }
                : new Dictionary<string, string> { ["StoreId"] = "STORE-004", ["UploadEndpoint"] = endpoint },
            @"C:\StoreSync\store.config");

        [Fact]
        public void A_healthy_store_answers_200_with_its_version()
        {
            var report = HealthReport.For(Settings("https://uploads.example.invalid/stores/STORE-004"), "3.3.0",
                                          new UploadSchedule(5), Started);

            Assert.Equal(200, report.StatusCode);
            Assert.Equal("{\"service\":\"store-sync\",\"version\":\"3.3.0\",\"status\":\"ok\",\"storeId\":\"STORE-004\"," +
                         "\"uploadEndpoint\":\"https://uploads.example.invalid/stores/STORE-004\",\"uploadIntervalMinutes\":5," +
                         "\"lastUploadAt\":null,\"startedAt\":\"2026-10-27T09:30:00Z\"}", report.Body);
        }

        [Fact]
        public void A_store_with_problems_answers_503_and_names_them()
        {
            var report = HealthReport.For(Settings("http://plain.example.invalid"), "3.3.0", new UploadSchedule(5), Started);

            Assert.Equal(503, report.StatusCode);
            Assert.Contains("\"status\":\"error\"", report.Body);
            Assert.Contains("\"version\":\"3.3.0\"", report.Body);
            Assert.Contains("\"problems\":[\"UploadEndpoint in C:\\\\StoreSync\\\\store.config is not an https address: http://plain.example.invalid\"]",
                            report.Body);
        }

        [Fact]
        public void The_last_upload_shows_once_there_is_one()
        {
            var schedule = new UploadSchedule(5);
            schedule.Uploaded(Started.AddMinutes(1));

            var report = HealthReport.For(Settings("https://uploads.example.invalid/stores/STORE-004"), "3.3.0", schedule, Started);

            Assert.Contains("\"lastUploadAt\":\"2026-10-27T09:31:00Z\"", report.Body);
        }

        [Theory]
        [InlineData("plain", "\"plain\"")]
        [InlineData("a \"quote\"", "\"a \\\"quote\\\"\"")]
        [InlineData(@"C:\StoreSync", "\"C:\\\\StoreSync\"")]
        [InlineData("line\nbreak", "\"line\\nbreak\"")]
        [InlineData("\u0001", "\"\\u0001\"")]
        public void Text_is_quoted_as_json(string text, string expected)
        {
            Assert.Equal(expected, HealthReport.Quote(text));
        }
    }
}
