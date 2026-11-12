# PyInstaller build spec for OpenQuantum Controls.
#
# Local build:   pyinstaller OpenQuantumControls.spec --noconfirm
# Output:        dist/OpenQuantumControls/   (plus dist/OpenQuantumControls.app on macOS)
#
# CI builds all three platforms — see .github/workflows/build.yml.

import sys

from PyInstaller.utils.hooks import collect_submodules

# A 3.12.0 bundle builds cleanly but dies on first connect: scipy.stats
# raises NameError: name 'obj' is not defined at import, frozen only.
# 3.12.10 builds a working one.
if sys.version_info[:3] == (3, 12, 0):
    raise SystemExit("Python 3.12.0 builds a broken bundle (scipy.stats "
                     "fails to import). Build with a later 3.12.")

a = Analysis(
    ["src/mot_control.py"],
    pathex=["src"],
    binaries=[],
    datas=[("fonts", "fonts"), ("assets", "assets")],
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
    # Windows only: on macOS PyInstaller would try to convert the .ico to
    # .icns, which needs Pillow and fails the build without it.
    icon="assets/icon.ico" if sys.platform == "win32" else None,
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
