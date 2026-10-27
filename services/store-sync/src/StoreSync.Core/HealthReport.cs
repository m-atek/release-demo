using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace StoreSync.Core
{
    /// <summary>
    /// What http://localhost:8080/health answers: 200 with the version when the
    /// store's settings are complete, 503 with the version and the problems
    /// when they are not. The pipeline's check reads both.
    /// </summary>
    public sealed class HealthReport
    {
        public int StatusCode { get; }
        public string Body { get; }

        private HealthReport(int statusCode, string body)
        {
            StatusCode = statusCode;
            Body = body;
        }

        public static HealthReport For(StoreSettings settings, string version, UploadSchedule schedule, DateTime startedUtc)
        {
            var fields = new List<KeyValuePair<string, object>>
            {
                Field("service", "store-sync"),
                Field("version", version),
                Field("status", settings.IsValid ? "ok" : "error"),
                Field("storeId", settings.StoreId),
                Field("uploadEndpoint", settings.UploadEndpoint),
                Field("uploadIntervalMinutes", (int)schedule.Interval.TotalMinutes),
                Field("lastUploadAt", schedule.LastUploadUtc.HasValue ? Iso(schedule.LastUploadUtc.Value) : null),
                Field("startedAt", Iso(startedUtc)),
            };
            if (!settings.IsValid)
                fields.Add(Field("problems", settings.Problems));
            return new HealthReport(settings.IsValid ? 200 : 503, Json(fields));
        }

        private static KeyValuePair<string, object> Field(string name, object value) => new KeyValuePair<string, object>(name, value);

        private static string Iso(DateTime utc) => utc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", CultureInfo.InvariantCulture);

        // netstandard2.0 has no JSON writer built in, and a package for one
        // object isn't worth it.
        private static string Json(IEnumerable<KeyValuePair<string, object>> fields)
        {
            var json = new StringBuilder("{");
            bool first = true;
            foreach (var field in fields)
            {
                if (!first) json.Append(',');
                first = false;
                json.Append(Quote(field.Key)).Append(':').Append(Value(field.Value));
            }
            return json.Append('}').ToString();
        }

        private static string Value(object value)
        {
            switch (value)
            {
                case null: return "null";
                case int number: return number.ToString(CultureInfo.InvariantCulture);
                case string text: return Quote(text);
                case IEnumerable<string> items:
                    var list = new List<string>();
                    foreach (var item in items) list.Add(Quote(item));
                    return "[" + string.Join(",", list) + "]";
                default: return Quote(value.ToString());
            }
        }

        public static string Quote(string text)
        {
            var quoted = new StringBuilder("\"");
            foreach (char c in text)
            {
                switch (c)
                {
                    case '"': quoted.Append("\\\""); break;
                    case '\\': quoted.Append("\\\\"); break;
                    case '\n': quoted.Append("\\n"); break;
                    case '\r': quoted.Append("\\r"); break;
                    case '\t': quoted.Append("\\t"); break;
                    default:
                        if (c < ' ') quoted.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else quoted.Append(c);
                        break;
                }
            }
            return quoted.Append('"').ToString();
        }
    }
}
