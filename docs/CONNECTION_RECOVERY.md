# Connection recovery on Windows

Run `scripts/enable_workbench_autostart.ps1` once to enable a per-user, hidden
watchdog at Windows sign-in. It stores no password and uses the existing SSH key
and Windows Credential Manager session. Storage remains the configured data directory.

The watchdog checks both model endpoints every ten seconds, restarts its SSH
tunnel after process exit or three failed health checks, and restarts the
workbench after process exit. It probes the remote ASR independently and can
restart the dedicated ASR wrapper. Shared text inference is never restarted.
Singleton file locking prevents duplicate watchdogs. Lifecycle and SSH errors
are recorded under runs/remote; model credentials are never logged.

Loopback transcription and text requests bypass Windows system proxies.
Self-hosted transcription allows up to 600 seconds for a response, with a
15-second connection limit. Public APIs retain normal proxy support.
Both partial and failed self-hosted Qwen ASR jobs can retry missing chunks,
at most twice per repair version; successful cached chunks are retained.

Recovery needs the PC awake, Windows signed in, network access to your model server and
available model services. It cannot process while the PC is powered off.
School sessions are validated on restart; genuine expiration still needs login.

To disable sign-in startup, remove only the fuckYanheWorkbench value
under HKCU/Software/Microsoft/Windows/CurrentVersion/Run. Creating
runs/remote/supervisor.stop stops the watchdog while leaving existing work
running; remove that file before enabling it again.

Example (use your own SSH alias and an optional data directory):

```powershell
./scripts/enable_workbench_autostart.ps1 -SshHost model-server -RemoteDirectory classroom-models -DataDirectory ./runs/webui
```

The installer records these non-secret options in `runs/remote/supervisor.json`.
Do not move the project after enabling startup without reinstalling the startup entry.
