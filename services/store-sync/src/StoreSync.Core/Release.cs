using System.Reflection;

namespace StoreSync.Core
{
    /// <summary>What changes between releases of store-sync.</summary>
    public static class Release
    {
        /// <summary>ORD-31: sales go up every 5 minutes instead of 15.</summary>
        public const int UploadIntervalMinutes = 5;

        /// <summary>
        /// Up to 3.2.0 a store without UploadEndpoint fell back to the central
        /// endpoint. Uploads every 5 minutes are too much for it, so 3.3.0
        /// needs each store's own regional endpoint: none is built in.
        /// </summary>
        public static readonly string BuiltInUploadEndpoint = null;

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
