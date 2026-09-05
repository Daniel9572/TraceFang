$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $request = $env:TRACEFANG_TASK_REQUEST | ConvertFrom-Json
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $name = "TraceFang-$identity"
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $folder = $scheduler.GetFolder('\')
    $task = $null
    try { $task = $folder.GetTask($name) }
    catch {
        if ($_.Exception.GetBaseException().HResult -ne -2147024894) { throw }
    }
    switch ($request.action) {
        'install' {
            $definition = $scheduler.NewTask(0)
            $definition.RegistrationInfo.Description = 'TraceFang local backend'
            $definition.Principal.UserId = $identity
            $definition.Principal.LogonType = 3 # Current user's interactive token; no password.
            $definition.Principal.RunLevel = 0
            $definition.Settings.Enabled = $true
            $definition.Settings.AllowDemandStart = $true
            $definition.Settings.DisallowStartIfOnBatteries = $false
            $definition.Settings.StopIfGoingOnBatteries = $false
            $definition.Settings.ExecutionTimeLimit = 'PT0S'
            $definition.Settings.MultipleInstances = 2 # IgnoreNew
            $definition.Settings.RestartInterval = 'PT1M'
            $definition.Settings.RestartCount = 3
            $definition.Settings.StartWhenAvailable = $true
            # Demand-start only: logging in must not start services without the app window.
            $action = $definition.Actions.Create(0)
            $action.Path = Join-Path $request.root '.venv\Scripts\pythonw.exe'
            $action.Arguments = '-m tracefang.service run'
            $action.WorkingDirectory = $request.root
            $task = $folder.RegisterTaskDefinition($name, $definition, 6, $identity, $null, 3, $null)
            $null = $task.Run($null)
        }
        'stop' {
            if ($null -ne $task) {
                $task.Enabled = $false
                if ($task.State -in @(2, 4)) { $task.Stop(0) }
                $deadline = [DateTime]::UtcNow.AddSeconds(30)
                while (($folder.GetTask($name)).State -in @(2, 4)) {
                    if ([DateTime]::UtcNow -gt $deadline) { throw 'Backend did not stop in time.' }
                    Start-Sleep -Milliseconds 100
                }
            }
        }
        'uninstall' {
            if ($null -ne $task) {
                $task.Enabled = $false
                if ($task.State -in @(2, 4)) { $task.Stop(0) }
                $folder.DeleteTask($name, 0)
                $task = $null
            }
        }
        'status' { }
        default { throw 'Unsupported task operation.' }
    }
    @{ running = ($null -ne $task -and $task.State -in @(2, 4)) } | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
