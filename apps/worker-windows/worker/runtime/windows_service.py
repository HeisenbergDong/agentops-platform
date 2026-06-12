from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

from worker.runtime.supervisor import SupervisorOptions, run_supervisor

SERVICE_WIN32_OWN_PROCESS = 0x00000010

SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004

SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000004

SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_SHUTDOWN = 0x00000005

NO_ERROR = 0
ERROR_SERVICE_SPECIFIC_ERROR = 1066
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063


def run_windows_service(
    service_name: str,
    options: SupervisorOptions,
    *,
    console_fallback: bool = False,
) -> int:
    if os.name != "nt":
        raise RuntimeError("Windows service mode is only available on Windows.")
    host = _WindowsServiceHost(service_name, options, console_fallback=console_fallback)
    return host.run()


class _WindowsServiceHost:
    def __init__(
        self,
        service_name: str,
        options: SupervisorOptions,
        *,
        console_fallback: bool,
    ) -> None:
        self.service_name = service_name
        self.options = options
        self.console_fallback = console_fallback
        self.stop_event = threading.Event()
        self.exit_code = 0
        self.checkpoint = 1
        self.status_handle = None
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._configure_api()
        self._handler = self._handler_type(self._control_handler)
        self._service_main = self._service_main_type(self._run_service)

    def run(self) -> int:
        service_table = self._build_service_table()

        if self.advapi32.StartServiceCtrlDispatcherW(service_table):
            return int(self.exit_code)

        error = ctypes.get_last_error()
        if error == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT and self.console_fallback:
            return run_supervisor(self.options, stop_event=self.stop_event)
        raise ctypes.WinError(error)

    def _build_service_table(self) -> object:
        service_table = (self._service_table_entry * 2)()
        service_table[0].lpServiceName = self.service_name
        service_table[0].lpServiceProc = self._service_main
        service_table[1].lpServiceName = None
        service_table[1].lpServiceProc = self._service_main_type(0)
        return service_table

    def _run_service(self, _argc: int, _argv: object) -> None:
        self.status_handle = self.advapi32.RegisterServiceCtrlHandlerW(self.service_name, self._handler)
        if not self.status_handle:
            self.exit_code = ctypes.get_last_error() or 1
            return

        self._set_status(SERVICE_START_PENDING, wait_hint_ms=30000)
        supervisor = threading.Thread(target=self._run_supervisor, name="agentops-worker-supervisor")
        supervisor.start()
        self._set_status(SERVICE_RUNNING)
        supervisor.join()
        self._set_status(SERVICE_STOPPED, service_exit_code=self.exit_code)

    def _run_supervisor(self) -> None:
        try:
            self.exit_code = run_supervisor(self.options, stop_event=self.stop_event)
        except Exception:
            self.exit_code = 1
            raise
        finally:
            self.stop_event.set()

    def _control_handler(self, control_code: int) -> int:
        if control_code in {SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN}:
            self._set_status(SERVICE_STOP_PENDING, wait_hint_ms=30000)
            self.stop_event.set()
            return NO_ERROR
        return NO_ERROR

    def _set_status(self, state: int, *, wait_hint_ms: int = 0, service_exit_code: int = 0) -> None:
        if not self.status_handle:
            return
        accepted = 0 if state in {SERVICE_START_PENDING, SERVICE_STOPPED} else SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
        checkpoint = 0
        if state in {SERVICE_START_PENDING, SERVICE_STOP_PENDING}:
            checkpoint = self.checkpoint
            self.checkpoint += 1
        win32_exit_code = ERROR_SERVICE_SPECIFIC_ERROR if service_exit_code else NO_ERROR
        status = self._service_status(
            dwServiceType=SERVICE_WIN32_OWN_PROCESS,
            dwCurrentState=state,
            dwControlsAccepted=accepted,
            dwWin32ExitCode=win32_exit_code,
            dwServiceSpecificExitCode=max(0, int(service_exit_code)),
            dwCheckPoint=checkpoint,
            dwWaitHint=wait_hint_ms,
        )
        self.advapi32.SetServiceStatus(self.status_handle, ctypes.byref(status))

    def _configure_api(self) -> None:
        self._handler_type = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD)
        self._service_main_type = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))

        class ServiceStatus(ctypes.Structure):
            _fields_ = [
                ("dwServiceType", wintypes.DWORD),
                ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD),
                ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD),
                ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD),
            ]

        class ServiceTableEntry(ctypes.Structure):
            _fields_ = [
                ("lpServiceName", wintypes.LPWSTR),
                ("lpServiceProc", self._service_main_type),
            ]

        self._service_status = ServiceStatus
        self._service_table_entry = ServiceTableEntry
        self.advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(ServiceTableEntry)]
        self.advapi32.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
        self.advapi32.RegisterServiceCtrlHandlerW.argtypes = [wintypes.LPCWSTR, self._handler_type]
        self.advapi32.RegisterServiceCtrlHandlerW.restype = wintypes.HANDLE
        self.advapi32.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(ServiceStatus)]
        self.advapi32.SetServiceStatus.restype = wintypes.BOOL
