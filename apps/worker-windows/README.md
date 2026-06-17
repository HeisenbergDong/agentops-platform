# AgentOps Windows Worker

This worker runs on the Windows machine that has Trae CN installed. The server only queues commands; this worker controls the local GUI, runs local checks, and posts results back.

## Register And Start

1. Open AgentOps Web as an admin.
2. Go to Worker and create a one-time registration code.
3. Double-click `agentops-worker.exe`.
4. If this machine is not registered yet, enter:

- AgentOps server URL, for example `http://115.190.113.8`
- The one-time worker registration code

After registration, the same window starts the worker runtime.

After registration, the worker saves its server URL, worker id, and token to:

```text
%APPDATA%\AgentOps\worker.json
```

Next time, start it by double-clicking:

```powershell
.\agentops-worker.exe
```

Keep the window open while the worker should stay online. Closing the window stops the worker. Reopen `agentops-worker.exe` to start it again.

For temporary end-to-end tests, start the worker in a visible window so the operator can see when it is running and close it after the test:

```powershell
.\scripts\start_worker_visible.ps1
```

This visible test launcher does not install autostart or a Windows service.

When the worker runtime starts, it automatically reuses an open Trae CN window or starts Trae CN by itself. It also retries this before sending each prompt. You do not need to open Trae manually. If Trae CN is installed in a nonstandard path, register with `--trae-exe-path` or set `AGENTOPS_WORKER_TRAE_EXE_PATH`.

For a single heartbeat and command poll:

```powershell
.\agentops-worker.exe --once
```

## Run As A Managed Worker

For normal Trae CN automation, use interactive logon autostart. This runs the worker in the signed-in desktop session, so it can control Trae CN and other GUI apps.

From the worker directory:

```powershell
.\scripts\install_worker_autostart.ps1 -RunNow
```

This creates a Scheduled Task named `AgentOpsWorker` that starts at user logon and runs:

```powershell
.\scripts\start_worker.ps1 -Supervise
```

The task runs as the current interactive user by default. If Trae CN was installed or started elevated and you really need matching permissions, add `-RunElevated` when installing the task.

The supervisor keeps the worker online, restarts it after crashes, and writes rotating logs to:

```text
%LOCALAPPDATA%\AgentOps\Worker\logs\agentops-worker.log
```

Defaults:

- Restart delay: 5 seconds
- Log rotation: 10 MiB per file
- Retained rotated logs: 5

Useful commands:

```powershell
.\scripts\status_worker.ps1
.\scripts\stop_worker.ps1
.\scripts\uninstall_worker_autostart.ps1
```

Tune supervision when installing:

```powershell
.\scripts\install_worker_autostart.ps1 `
  -RestartDelaySeconds 10 `
  -LogMaxMB 20 `
  -LogBackups 10 `
  -RunNow
```

## Windows Service Mode

A real Windows Service entry is also available for environments that need SCM-managed startup:

```powershell
# Run in elevated PowerShell
.\scripts\install_worker_service.ps1 -Start
```

Windows services run in Session 0 and normally cannot control the interactive Trae CN desktop. For the main GUI automation path, prefer `install_worker_autostart.ps1`. Use service mode only for diagnostics or non-GUI worker commands.

Service operations:

```powershell
.\scripts\status_worker.ps1
.\scripts\stop_worker.ps1
.\scripts\uninstall_worker_service.ps1
```

## Optional Paths

If Trae CN or the workspace root is not in the default location, pass them during registration:

```powershell
.\agentops-worker.exe register `
  --server-url http://115.190.113.8 `
  --registration-code <code> `
  --trae-exe-path "D:\app\Trae CN\Trae CN.exe" `
  --workspace-root "D:\code-space\coding-soler" `
  --start
```

Use a portable config path when needed:

```powershell
.\agentops-worker.exe --config .\worker.json
```

## Build Package

From `apps\worker-windows` on a Windows build machine:

```powershell
.\scripts\build_worker.ps1 -Clean
```

Outputs:

- `dist\agentops-worker-windows\agentops-worker.exe`
- `dist\agentops-worker-windows.zip`

The EXE bundles Python and worker dependencies. The target machine still needs Trae CN, Git, and any project runtimes required by generated projects, such as Node.js, Python, Go, Maven, or Java.

## Server Binding

Registration only creates the worker record and token. The admin or user still needs to bind the worker to the target user and set the user's worker settings in AgentOps Web.

The worker applies the bound user's Trae workspace path and browser acceptance URL from the server on every heartbeat. These runtime values do not overwrite the local registration token file; command payload values still take precedence for a single command.
