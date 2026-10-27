using System;
using System.ServiceProcess;

namespace StoreSync
{
    /// <summary>
    /// Runs as the Windows service StoreSync. With --console it runs in the
    /// foreground until Enter, for a quick look on a server.
    /// </summary>
    internal static class Program
    {
        private static void Main(string[] args)
        {
            if (Array.IndexOf(args, "--console") >= 0)
            {
                var service = new StoreSyncService();
                service.Start();
                Console.WriteLine("store-sync is running on http://localhost:8080/health. Press Enter to stop.");
                Console.ReadLine();
                service.Halt();
                return;
            }
            ServiceBase.Run(new StoreSyncService());
        }
    }
}
