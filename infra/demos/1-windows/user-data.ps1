<powershell>
# First boot of a store server (EC2Launch v2). The only thing done outside Run
# Command: a scheduled task that shuts the server down 4 hours after every
# boot, so a forgotten fleet costs only its disks. Shutdown stops the
# instance (instance_initiated_shutdown_behavior = stop).
$action = New-ScheduledTaskAction -Execute 'shutdown.exe' -Argument '/s /t 0 /c "Stops 4 hours after boot"'
$atStartup = New-ScheduledTaskTrigger -AtStartup
$atStartup.Delay = 'PT4H'
$thisBoot = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(4)
Register-ScheduledTask -TaskName 'StoreFleetAutoStop' -Action $action -Trigger @($atStartup, $thisBoot) `
    -User 'SYSTEM' -RunLevel Highest -Force `
    -Description 'Stops this server 4 hours after boot.' | Out-Null
</powershell>
