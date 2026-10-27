using System;
using System.Collections.Generic;

namespace StoreSync.Core
{
    /// <summary>
    /// The store's own settings: StoreId and UploadEndpoint, from the store's
    /// config file, which stays in place when a new version is installed next
    /// to the old one. Problems are collected, not thrown, so /health can say
    /// what is wrong.
    /// </summary>
    public sealed class StoreSettings
    {
        public string StoreId { get; }
        public string UploadEndpoint { get; }
        public bool UsesBuiltInEndpoint { get; }
        public IReadOnlyList<string> Problems { get; }
        public bool IsValid => Problems.Count == 0;

        private StoreSettings(string storeId, string uploadEndpoint, bool builtIn, IReadOnlyList<string> problems)
        {
            StoreId = storeId;
            UploadEndpoint = uploadEndpoint;
            UsesBuiltInEndpoint = builtIn;
            Problems = problems;
        }

        /// <param name="values">The appSettings the service read.</param>
        /// <param name="source">Where they came from, named in every problem.</param>
        public static StoreSettings From(IDictionary<string, string> values, string source)
        {
            var problems = new List<string>();
            string storeId = Value(values, "StoreId");
            string endpoint = Value(values, "UploadEndpoint");
            bool builtIn = false;

            if (storeId == null)
                problems.Add($"StoreId is not set in {source}");

            if (endpoint == null && Release.BuiltInUploadEndpoint != null)
            {
                endpoint = Release.BuiltInUploadEndpoint;
                builtIn = true;
            }
            if (endpoint == null)
                problems.Add($"UploadEndpoint is not set in {source}");
            else if (!Uri.TryCreate(endpoint, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps)
                problems.Add($"UploadEndpoint in {source} is not an https address: {endpoint}");

            return new StoreSettings(storeId, endpoint, builtIn, problems);
        }

        private static string Value(IDictionary<string, string> values, string key)
        {
            return values != null && values.TryGetValue(key, out var value) && !string.IsNullOrWhiteSpace(value)
                ? value.Trim()
                : null;
        }
    }
}
