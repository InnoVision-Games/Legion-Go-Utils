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

    file: common/lib/__init__.py

    Marks common/lib/ as a package. Holds shared HOST-SIDE Python helper
    modules -- dkms_supported_versions.py and package_downloader.py --
    imported normally (`from common.lib.package_downloader import
    PackageDownloader`, etc.) by acpi_enabler.py and/or
    nvidia_usb_image_builder.py. This is the deliberate counterpart to
    common/selfheal/, which holds on-device payload scripts that are
    copied verbatim rather than imported -- see common/__init__.py for
    the split.
'''

"""Package init: common/lib/ holds shared host-side Python helper modules."""
