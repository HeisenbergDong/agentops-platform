# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['worker\\main.py'],
    pathex=['D:\\code-space\\auto-tool\\agentops-platform-stop-trace-fix\\apps\\worker-windows'],
    binaries=[],
    datas=[],
    hiddenimports=['comtypes', 'comtypes.client', 'comtypes.gen.UIAutomationClient', 'mss', 'PIL.Image'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='agentops-worker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
