using System;

namespace StoreSync.Core
{
    /// <summary>When the store's next sales upload is due.</summary>
    public sealed class UploadSchedule
    {
        public TimeSpan Interval { get; }
        public DateTime? LastUploadUtc { get; private set; }

        public UploadSchedule(int intervalMinutes)
        {
            if (intervalMinutes <= 0)
                throw new ArgumentOutOfRangeException(nameof(intervalMinutes), "The interval must be at least a minute.");
            Interval = TimeSpan.FromMinutes(intervalMinutes);
        }

        /// <summary>The first upload is due at once, then one every interval.</summary>
        public bool IsDue(DateTime nowUtc) => LastUploadUtc == null || nowUtc - LastUploadUtc.Value >= Interval;

        public DateTime NextUploadUtc(DateTime nowUtc) => LastUploadUtc == null ? nowUtc : LastUploadUtc.Value + Interval;

        public void Uploaded(DateTime atUtc) => LastUploadUtc = atUtc;
    }
}
