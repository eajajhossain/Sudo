param([string]$WavPath = "", [int]$Seconds = 12)
# Local speech to text using the recognizer built into Windows. Nothing leaves the PC.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$installed = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
if ($installed.Count -eq 0) { [Console]::Error.WriteLine("no-recognizer"); exit 3 }
$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($installed[0])
$engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
if ($WavPath) { $engine.SetInputToWaveFile($WavPath) } else { $engine.SetInputToDefaultAudioDevice() }
$engine.InitialSilenceTimeout = [TimeSpan]::FromSeconds(6)
$engine.EndSilenceTimeout = [TimeSpan]::FromSeconds(1)
$result = $engine.Recognize([TimeSpan]::FromSeconds($Seconds))
if ($result) { Write-Output $result.Text }
$engine.Dispose()
