using System.Reflection;

namespace StoreSync.Core
{
    /// <summary>What changes between releases of store-sync.</summary>
    public static class Release
    {
        /// <summary>Sales go up every 15 minutes.</summary>
        public const int UploadIntervalMinutes = 15;

        /// <summary>
        /// A store without UploadEndpoint in its config uploads to the central
        /// endpoint.
        /// </summary>
        public static readonly string BuiltInUploadEndpoint = "https://uploads.store-sync.internal/central";

        /// <summary>The version from VERSION, which the build stamps into the assembly.</summary>
        public static string Version
        {
            get
            {
                var attribute = typeof(Release).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>();
                return attribute != null ? attribute.InformationalVersion : "unknown";
            }
        }
    }
}
