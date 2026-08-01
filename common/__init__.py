#!/usr/bin/env python3

'''
    MIT License

    Copyright (c) 2026 InnoVision Games

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

    file: common/__init__.py

    Marks common/ as a package. It is an umbrella over two subpackages
    that hold two genuinely different kinds of shared code -- kept
    apart deliberately rather than flattened together:

    - common/selfheal/: the on-device self-healing update machinery
      shared by AcpiEnabler and NvidiaUsbImageBuilder -- the repatch.py
      script (repatch_script.py) that rebuilds whichever of the NVIDIA
      driver / acpi_call are configured after an OS update, and the
      update wrapper (update_wrapper.py) that triggers it. Both ship as
      real standalone scripts -- REPATCH_SCRIPT/UPDATE_WRAPPER_SCRIPT
      class constants on each payload-specific class point here and
      shutil.copy() them into place verbatim; NEITHER is ever imported
      as a Python module.

    - common/lib/: shared HOST-SIDE Python helper modules --
      dkms_supported_versions.py and package_downloader.py -- imported
      normally (e.g. `from common.lib.package_downloader import
      PackageDownloader`) by acpi_enabler.py and/or
      nvidia_usb_image_builder.py.

    Neither payload-specific class owns either subpackage -- they live
    here precisely because more than one class depends on them equally.
'''

"""Package init: common/ is an umbrella over common/selfheal/ (on-device
self-heal payload scripts) and common/lib/ (shared host-side Python
helper modules)."""
