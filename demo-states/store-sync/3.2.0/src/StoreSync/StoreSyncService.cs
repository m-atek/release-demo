using System;
using System.Collections.Generic;
using System.Configuration;
using System.IO;
using System.Net;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using StoreSync.Core;

namespace StoreSync
{
    /// <summary>
    /// The store-sync Windows service: uploads the store's sales on a schedule
    /// and answers http://localhost:8080/health. It stands in for legacy store
    /// software, so an upload only writes a line to its log; nothing leaves
    /// the server.
    /// </summary>
    public sealed class StoreSyncService : ServiceBase
    {
        private const string HealthPrefix = "http://localhost:8080/";
        private static readonly string Root = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
        private static readonly string ConfigFile = Path.Combine(Root, "store.config");
        private static readonly string LogFile = Path.Combine(Root, "logs", "store-sync.log");

        private readonly UploadSchedule _schedule = new UploadSchedule(Release.UploadIntervalMinutes);
        private readonly object _lock = new object();
        private HttpListener _listener;
        private Timer _timer;
        private DateTime _startedUtc;

        public StoreSyncService()
        {
            ServiceName = "StoreSync";
            CanStop = true;
            AutoLog = true;
        }

        protected override void OnStart(string[] args) => Start();

        protected override void OnStop() => Halt();

        public void Start()
        {
            _startedUtc = DateTime.UtcNow;
            var settings = ReadSettings();
            Log($"store-sync {Release.Version} started for {settings.StoreId ?? "an unknown store"}, " +
                $"uploads every {Release.UploadIntervalMinutes} minutes");
            foreach (var problem in settings.Problems)
                Log("Config problem: " + problem);

            _listener = new HttpListener();
            _listener.Prefixes.Add(HealthPrefix);
            _listener.Start();
            _listener.BeginGetContext(Answer, null);
            _timer = new Timer(_ => Tick(), null, TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(30));
        }

        public void Halt()
        {
            _timer?.Dispose();
            if (_listener != null && _listener.IsListening)
                _listener.Stop();
            Log($"store-sync {Release.Version} stopped");
        }

        /// <summary>Read on every use, so a fixed config shows without a restart.</summary>
        private static StoreSettings ReadSettings()
        {
            ConfigurationManager.RefreshSection("appSettings");
            var values = new Dictionary<string, string>();
            foreach (string key in ConfigurationManager.AppSettings.AllKeys)
                values[key] = ConfigurationManager.AppSettings[key];
            return StoreSettings.From(values, ConfigFile);
        }

        private void Tick()
        {
            lock (_lock)
            {
                var now = DateTime.UtcNow;
                var settings = ReadSettings();
                if (!settings.IsValid || !_schedule.IsDue(now))
                    return;
                _schedule.Uploaded(now);
                Log($"Uploaded the sales of {settings.StoreId} to {settings.UploadEndpoint}");
            }
        }

        private void Answer(IAsyncResult result)
        {
            HttpListenerContext context;
            try
            {
                context = _listener.EndGetContext(result);
                _listener.BeginGetContext(Answer, null);
            }
            catch (Exception error) when (error is HttpListenerException || error is ObjectDisposedException)
            {
                return; // stopping
            }

            try
            {
                HealthReport report;
                lock (_lock)
                    report = HealthReport.For(ReadSettings(), Release.Version, _schedule, _startedUtc);
                bool health = context.Request.Url.AbsolutePath.TrimEnd('/') == "/health";
                byte[] body = Encoding.UTF8.GetBytes(health ? report.Body : "{\"error\":\"not found\"}");
                context.Response.StatusCode = health ? report.StatusCode : 404;
                context.Response.ContentType = "application/json";
                context.Response.ContentLength64 = body.Length;
                context.Response.OutputStream.Write(body, 0, body.Length);
                context.Response.Close();
            }
            catch (HttpListenerException)
            {
                // The caller went away before the answer: nothing to do.
            }
        }

        private static void Log(string line)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(LogFile));
                File.AppendAllText(LogFile, $"{DateTime.UtcNow:yyyy-MM-dd'T'HH:mm:ss'Z'} {line}{Environment.NewLine}");
            }
            catch (IOException)
            {
                // A log that can't be written never stops the service.
            }
        }
    }
}
