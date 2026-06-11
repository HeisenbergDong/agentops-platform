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

When the worker runtime starts, it automatically reuses an open Trae CN window or starts Trae CN by itself. It also retries this before sending each prompt. You do not need to open Trae manually. If Trae CN is installed in a nonstandard path, register with `--trae-exe-path` or set `AGENTOPS_WORKER_TRAE_EXE_PATH`.

For a single heartbeat and command poll:

```powershell
.\agentops-worker.exe --once
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
