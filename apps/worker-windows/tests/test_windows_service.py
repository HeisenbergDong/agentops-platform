import os

import pytest

from worker.runtime.supervisor import SupervisorOptions


@pytest.mark.skipif(os.name != "nt", reason="Windows service host uses Windows SCM APIs")
def test_windows_service_table_uses_null_terminator():
    from worker.runtime.windows_service import _WindowsServiceHost

    host = _WindowsServiceHost(
        "AgentOpsWorkerTest",
        SupervisorOptions(require_registered=False),
        console_fallback=True,
    )
    table = host._build_service_table()

    assert table[0].lpServiceName == "AgentOpsWorkerTest"
    assert table[1].lpServiceName is None
    assert bool(table[1].lpServiceProc) is False
