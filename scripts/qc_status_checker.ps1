param(
    [Parameter(Mandatory = $true)]
    [string]$ConnectionString,

    [string]$RunnerName = $env:COMPUTERNAME
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-QcStatus {
    param(
        [Parameter(Mandatory = $true)]
        [System.Data.Odbc.OdbcConnection]$Connection,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [ValidateSet("Working", "Down")]
        [string]$QcStatus
    )

    $sql = "UPDATE rpa_runner SET QC_Status = ?, QC_Time = NOW(), Update_Time = NOW() WHERE RunnerName = ?"
    $cmd = New-Object System.Data.Odbc.OdbcCommand($sql, $Connection)
    [void]$cmd.Parameters.Add((New-Object System.Data.Odbc.OdbcParameter("", $QcStatus)))
    [void]$cmd.Parameters.Add((New-Object System.Data.Odbc.OdbcParameter("", $Name)))
    [void]$cmd.ExecuteNonQuery()
}

function Test-BotWindowRunning {
    # Returns $true when any process window title contains "Bot running..."
    $javaw = Get-Process javaw -ErrorAction SilentlyContinue
    foreach ($p in $javaw) {
        if ($p.MainWindowTitle -like "*Bot running...*") { return $true }
    }

    $launcher = Get-Process BotLauncher -ErrorAction SilentlyContinue
    foreach ($p in $launcher) {
        if ($p.MainWindowTitle -like "*Bot running...*") { return $true }
    }

    return $false
}

$connection = New-Object System.Data.Odbc.OdbcConnection($ConnectionString)

try {
    $connection.Open()

    $readSql = "SELECT TaskBotStatus FROM rpa_runner WHERE RunnerName = ? LIMIT 1"
    $readCmd = New-Object System.Data.Odbc.OdbcCommand($readSql, $connection)
    [void]$readCmd.Parameters.Add((New-Object System.Data.Odbc.OdbcParameter("", $RunnerName)))
    $reader = $readCmd.ExecuteReader()

    if (-not $reader.Read()) {
        throw "RunnerName '$RunnerName' not found in rpa_runner"
    }

    $taskBotStatus = [string]$reader["TaskBotStatus"]
    $reader.Close()

    if ($taskBotStatus -match "DISABLE") {
        Write-Output "Runner '$RunnerName' is DISABLE. Skip QC update."
        exit 0
    }

    $isRunning = Test-BotWindowRunning
    if ($isRunning) {
        Write-QcStatus -Connection $connection -Name $RunnerName -QcStatus "Working"
        Write-Output "QC updated: Working ($RunnerName)"
    }
    else {
        Write-QcStatus -Connection $connection -Name $RunnerName -QcStatus "Down"
        Write-Output "QC updated: Down ($RunnerName)"
    }
}
finally {
    if ($connection.State -eq [System.Data.ConnectionState]::Open) {
        $connection.Close()
    }
}
