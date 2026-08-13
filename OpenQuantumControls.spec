# PyInstaller build spec for OpenQuantum Controls.
#
# Local build:   pyinstaller OpenQuantumControls.spec --noconfirm
# Output:        dist/OpenQuantumControls/   (plus dist/OpenQuantumControls.app on macOS)
#
# CI builds all three platforms — see .github/workflows/build.yml.

import sys

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["mot_control.py"],
    pathex=["."],
    binaries=[],
    datas=[("fonts", "fonts")],
    hiddenimports=[
        # linien-client reaches parts of these through rpyc's dynamic
        # machinery, so collect them wholesale.
        *collect_submodules("linien_client"),
        *collect_submodules("linien_common"),
        *collect_submodules("rpyc"),
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide6"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="OpenQuantumControls",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="OpenQuantumControls",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="OpenQuantumControls.app",
        icon=None,
        bundle_identifier="org.openquantum.controls",
        info_plist={
            "NSHighResolutionCapable": True,
            # The optional camera pane needs this or macOS kills the app
            # when the user enables it.
            "NSCameraUsageDescription": (
                "OpenQuantum Controls shows the MOT USB camera."
            ),
        },
    )
