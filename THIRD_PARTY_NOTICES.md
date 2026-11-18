# Third-party notices

Duplicate Photo Finder uses the work of other authors. This file lists it and keeps the notices their licenses require.

## SSCD model

`dedup/models/sscd_int8.onnx` is the `sscd_disc_mixup` model from SSCD, converted to ONNX and quantized to int8.

- Project: https://github.com/facebookresearch/sscd-copy-detection
- Paper: Ed Pizzi, Sreya Dutta Roy, Sugosh Nagavara Ravindra, Priya Goyal, Matthijs Douze. A Self-Supervised Descriptor for Image Copy Detection. CVPR 2022. https://arxiv.org/abs/2202.10261
- License: MIT (the project was relicensed from CC BY-NC 4.0 to MIT on 2022-07-14). The weights are published in the same repository and carry no separate license.

```
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Python libraries

Bundled into the Mac and Windows builds.

| Library | License | Project |
|---|---|---|
| numpy | BSD-3-Clause and others | https://numpy.org |
| Pillow | MIT-CMU | https://python-pillow.org |
| pillow-heif | BSD-3-Clause | https://github.com/bigcat88/pillow_heif |
| opencv-python-headless | Apache-2.0 | https://github.com/opencv/opencv-python |
| onnxruntime | MIT | https://github.com/microsoft/onnxruntime |
| pdqhash | MIT | https://github.com/faustomorales/pdqhash-python |
| pystray | LGPL-3.0 | https://github.com/moses-palmer/pystray |
| six (Windows build) | MIT | https://github.com/benjaminp/six |
| PyObjC: pyobjc-core, pyobjc-framework-Cocoa, pyobjc-framework-Quartz (Mac build) | MIT | https://github.com/ronaldoussoren/pyobjc |

pystray draws the program's icon by the clock. It is included unchanged, as its own source files, so it can be replaced with another version: in the Windows build it is the folder `python/Lib/site-packages/pystray`, in the Mac build the folder `pystray` inside `Duplicate Photo Finder.app/Contents/Resources`. Its source and the full license text: https://github.com/moses-palmer/pystray.

The pillow-heif binary wheels contain libheif and libde265 (LGPL-3.0) and x265 (GPL-2.0). Their sources are linked from the pillow-heif project. The source of this program is in this repository.

## Also bundled in the builds

- Python (PSF License): https://www.python.org. The Mac build uses python-build-standalone: https://github.com/astral-sh/python-build-standalone
- PyInstaller bootloader (GPL-2.0 with an exception that allows distributing the built app under any license): https://pyinstaller.org
- Microsoft Visual C++ runtime files `msvcp140.dll`, `msvcp140_1.dll` (Windows build), redistributable under the Microsoft Visual C++ Redistributable license.
