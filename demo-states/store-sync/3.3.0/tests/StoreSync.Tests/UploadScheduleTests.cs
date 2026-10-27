using System;
using StoreSync.Core;
using Xunit;

namespace StoreSync.Tests
{
    public class UploadScheduleTests
    {
        private static readonly DateTime Noon = new DateTime(2026, 10, 27, 12, 0, 0, DateTimeKind.Utc);

        [Fact]
        public void The_release_sets_the_interval()
        {
            var schedule = new UploadSchedule(Release.UploadIntervalMinutes);

            Assert.Equal(TimeSpan.FromMinutes(Release.UploadIntervalMinutes), schedule.Interval);
        }

        [Fact]
        public void The_first_upload_is_due_at_once()
        {
            var schedule = new UploadSchedule(5);

            Assert.True(schedule.IsDue(Noon));
            Assert.Equal(Noon, schedule.NextUploadUtc(Noon));
        }

        [Fact]
        public void The_next_upload_waits_one_interval()
        {
            var schedule = new UploadSchedule(5);
            schedule.Uploaded(Noon);

            Assert.False(schedule.IsDue(Noon.AddMinutes(4).AddSeconds(59)));
            Assert.True(schedule.IsDue(Noon.AddMinutes(5)));
            Assert.Equal(Noon.AddMinutes(5), schedule.NextUploadUtc(Noon));
        }

        [Fact]
        public void An_interval_under_a_minute_is_refused()
        {
            Assert.Throws<ArgumentOutOfRangeException>(() => new UploadSchedule(0));
        }
    }
}
