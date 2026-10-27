using System.Collections.Generic;
using StoreSync.Core;
using Xunit;

namespace StoreSync.Tests
{
    public class StoreSettingsTests
    {
        private const string Source = @"C:\StoreSync\store.config";

        private static Dictionary<string, string> Config(string storeId, string endpoint)
        {
            var values = new Dictionary<string, string>();
            if (storeId != null) values["StoreId"] = storeId;
            if (endpoint != null) values["UploadEndpoint"] = endpoint;
            return values;
        }

        [Fact]
        public void A_complete_config_is_valid()
        {
            var settings = StoreSettings.From(Config("STORE-001", "https://uploads.example.invalid/stores/STORE-001"), Source);

            Assert.True(settings.IsValid);
            Assert.Equal("STORE-001", settings.StoreId);
            Assert.Equal("https://uploads.example.invalid/stores/STORE-001", settings.UploadEndpoint);
            Assert.False(settings.UsesBuiltInEndpoint);
        }

        [Fact]
        public void Values_are_trimmed()
        {
            var settings = StoreSettings.From(Config("  STORE-002 ", " https://uploads.example.invalid/stores/STORE-002 "), Source);

            Assert.Equal("STORE-002", settings.StoreId);
            Assert.Equal("https://uploads.example.invalid/stores/STORE-002", settings.UploadEndpoint);
        }

        [Fact]
        public void A_missing_store_id_is_a_problem()
        {
            var settings = StoreSettings.From(Config(null, "https://uploads.example.invalid/stores/STORE-003"), Source);

            Assert.False(settings.IsValid);
            Assert.Contains($"StoreId is not set in {Source}", settings.Problems);
        }

        [Fact]
        public void A_missing_upload_endpoint_follows_the_release()
        {
            var settings = StoreSettings.From(Config("STORE-004", null), Source);

            if (Release.BuiltInUploadEndpoint == null)
            {
                Assert.False(settings.IsValid);
                Assert.Contains($"UploadEndpoint is not set in {Source}", settings.Problems);
            }
            else
            {
                Assert.True(settings.IsValid);
                Assert.True(settings.UsesBuiltInEndpoint);
                Assert.Equal(Release.BuiltInUploadEndpoint, settings.UploadEndpoint);
            }
        }

        [Fact]
        public void An_empty_upload_endpoint_counts_as_missing()
        {
            var settings = StoreSettings.From(Config("STORE-004", "   "), Source);

            Assert.Equal(Release.BuiltInUploadEndpoint == null, !settings.IsValid);
        }

        [Theory]
        [InlineData("http://uploads.example.invalid/stores/STORE-005")]
        [InlineData("uploads.example.invalid")]
        public void The_upload_endpoint_must_be_https(string endpoint)
        {
            var settings = StoreSettings.From(Config("STORE-005", endpoint), Source);

            Assert.False(settings.IsValid);
            Assert.Contains($"UploadEndpoint in {Source} is not an https address: {endpoint}", settings.Problems);
        }

        [Fact]
        public void No_values_at_all_gives_every_problem()
        {
            var settings = StoreSettings.From(null, Source);

            Assert.False(settings.IsValid);
            Assert.Contains($"StoreId is not set in {Source}", settings.Problems);
        }
    }
}
