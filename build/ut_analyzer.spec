# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for UT Analyzer Windows release build."""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None
_spec_dir = Path(SPECPATH).resolve()
if _spec_dir.suffix == ".spec":
    _spec_dir = _spec_dir.parent
project_root = _spec_dir.parent

_tslp_datas, _tslp_binaries, _tslp_hiddenimports = collect_all("tree_sitter_language_pack")

a = Analysis(
    [str(project_root / "run.py")],
    pathex=[str(project_root)],
    binaries=_tslp_binaries,
    datas=[
        (str(project_root / "release" / "versions.json"), "default-config"),
        (str(project_root / "licenses"), "licenses"),
        *_tslp_datas,
    ],
    hiddenimports=[
        "ut_analyzer",
        "ut_analyzer.main",
        "ut_analyzer.adapters.base",
        "ut_analyzer.adapters.cpp.adapter",
        "ut_analyzer.adapters.cpp.frameworks",
        "ut_analyzer.adapters.cpp.build_systems",
        "ut_analyzer.adapters.python.adapter",
        "ut_analyzer.adapters.javascript.adapter",
        "ut_analyzer.adapters.java.adapter",
        "ut_analyzer.adapters.csharp.adapter",
        "ut_analyzer.adapters.go.adapter",
        "ut_analyzer.core.detection.framework_hints",
        "ut_analyzer.core.detection.test_content_sampler",
        "ut_analyzer.core.discovery.test_file_resolver",
        "ut_analyzer.core.quality.quality_analyzer",
        "tree_sitter",
        "tree_sitter_language_pack",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        *_tslp_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="UTAnalyzer-1.3",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
