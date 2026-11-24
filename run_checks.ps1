param(
    [string]$Prefix,
    [switch]$SkipRadio
)

$ErrorActionPreference = 'Stop'
$oldPath = $env:Path
$checks = New-Object System.Collections.Generic.List[object]

function Invoke-Check($Name, $Executable, $Arguments, [scriptblock]$Validate) {
    $stdout = Join-Path $logDir "$Name.stdout.log"
    $stderr = Join-Path $logDir "$Name.stderr.log"
    $status = 1
    try {
        $task = Start-Process -FilePath $Executable -ArgumentList $Arguments `
            -WorkingDirectory $PSScriptRoot -Wait -PassThru -NoNewWindow `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $status = $task.ExitCode
        if ($status -eq 0 -and $Validate) { & $Validate }
    } catch {
        $status = 1
        Write-Warning "$Name $($_.Exception.Message)"
    }
    $checks.Add([pscustomobject]@{ Name = $Name; ExitCode = $status
        Stdout = "output/checks/$Name.stdout.log"; Stderr = "output/checks/$Name.stderr.log" })
    Write-Output "$Name exit_code=$status"
    if ($status -ne 0 -and (Test-Path -LiteralPath $stderr)) {
        $log = [IO.File]::OpenRead($stderr)
        try {
            $null = $log.Seek([Math]::Max(0, $log.Length - 4000), [IO.SeekOrigin]::Begin)
            $reader = [IO.StreamReader]::new($log)
            $reader.ReadToEnd()
        } finally {
            $log.Dispose()
        }
    }
}

Push-Location -LiteralPath $PSScriptRoot
try {
    if ($Prefix) {
        $Prefix = (Resolve-Path -LiteralPath $Prefix).Path
        $env:Path = "$Prefix;$Prefix\Library\bin;$Prefix\Scripts;$oldPath"
        $python = Join-Path $Prefix 'python.exe'
    } else {
        $python = (Get-Command python.exe -CommandType Application | Select-Object -First 1).Source
    }
    $logDir = Join-Path $PSScriptRoot 'output\checks'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    foreach ($mode in 'dsb-sc', 'am', 'fm') {
        Invoke-Check $mode $python @('programs/modulation_demo.py', '--mode', $mode)
    }
    Invoke-Check 'receiver-synthetic' $python @('programs/fm_receiver.py', '--self-test')
    Invoke-Check 'delay-synthetic' $python @('programs/measure_delay.py', '--self-test')
    Invoke-Check 'delay-recording' $python @('programs/measure_delay.py', 'results/fm/cnr-1061.wav',
        '--stereo', '--output', 'output/checks/delay.json')
    Invoke-Check 'rf-recording' $python @('programs/plot_rf.py', 'results/fm/rtl-probe.cu8',
        '--output', 'output/checks/rf-1061.png')
    if (-not $SkipRadio) {
        Invoke-Check 'rtl-probe' 'rtl_sdr.exe' @('-f', '106350000', '-s', '2400000', '-n', '4800000',
            'output/checks/rtl-probe.cu8') {
            if ((Get-Item -LiteralPath 'output/checks/rtl-probe.cu8').Length -ne 9600000) {
                throw 'Expected 4,800,000 pairs of unsigned I/Q bytes.'
            }
        }
        Invoke-Check 'receiver-live' $python @('programs/fm_receiver.py', '--no-gui', '--no-audio',
            '--seconds', '12', '--wav', 'output/checks/fm-1061.wav') {
            if ((Get-Item -LiteralPath 'output/checks/fm-1061.wav').Length -lt 1152000) {
                throw 'Expected 12 seconds of demodulated 48 kHz PCM audio.'
            }
        }
    }
    $checks | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logDir 'summary.json') -Encoding UTF8
} finally {
    Pop-Location
    $env:Path = $oldPath
}
if ($checks | Where-Object ExitCode -ne 0) { exit 1 }
