# PyInstaller recipe for the Mac app "Duplicate Photo Finder.app" (Apple Silicon). Built by packaging/build_mac.sh.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = open(os.path.join(ROOT, "VERSION")).read().strip()

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, "dedup", "ui", "index.html"), os.path.join("dedup", "ui")),
        (os.path.join(ROOT, "dedup", "models", "sscd_int8.onnx"), os.path.join("dedup", "models")),
    ],
    hiddenimports=["dedup.__main__", "dedup.ui.server"],
    # onnxruntime pulls sympy only for its offline tools; the app never imports it
    excludes=["tkinter", "sympy", "mpmath", "matplotlib", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DuplicatePhotoFinder", console=False, target_arch="arm64")
coll = COLLECT(exe, a.binaries, a.datas, name="DuplicatePhotoFinder")
app = BUNDLE(
    coll,
    name="Duplicate Photo Finder.app",
    bundle_identifier="io.github.dwenlvov.duplicate-photo-finder",
    info_plist={
        "CFBundleName": "Duplicate Photo Finder",
        "CFBundleDisplayName": "Duplicate Photo Finder",
        "CFBundleDevelopmentRegion": "en",
        "CFBundleShortVersionString": VERSION,
        # onnxruntime 1.24 for Apple Silicon is built for macOS 14; everything else allows older systems
        "LSMinimumSystemVersion": "14.0",
        # no window of its own: the page opens in the browser, the app is switched off by a button on the page
        "LSUIElement": True,
    },
)
