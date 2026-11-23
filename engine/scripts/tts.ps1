<#
.SYNOPSIS
  Speak a list of lines with Windows' built-in voices, one WAV per line.
.DESCRIPTION
  Used to build synthetic test meetings that are safe to publish. Reads a JSON
  array of { "voice": "...", "text": "..." } and writes 000.wav, 001.wav, ...
  Run it with PowerShell 7 (pwsh): it can use the newer voices (Microsoft David,
  Mark, Zira), while Windows PowerShell 5.1 only sees the two "Desktop" ones.
#>
param(
    [Parameter(Mandatory)] [string] $ScriptJson,
    [Parameter(Mandatory)] [string] $OutDir,
    [int] $Rate = 16000
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

$lines = Get-Content -Raw -Encoding UTF8 $ScriptJson | ConvertFrom-Json
New-Item -ItemType Directory -Force $OutDir | Out-Null

$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo -ArgumentList $Rate,
    ([System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen),
    ([System.Speech.AudioFormat.AudioChannel]::Mono)

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $i = 0
    foreach ($line in $lines) {
        $synth.SelectVoice($line.voice)
        $synth.SetOutputToWaveFile((Join-Path $OutDir ('{0:d3}.wav' -f $i)), $format)
        $synth.Speak($line.text)
        $synth.SetOutputToNull()
        $i++
    }
}
finally {
    $synth.Dispose()
}
