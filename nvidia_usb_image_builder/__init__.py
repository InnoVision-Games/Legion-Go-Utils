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

    file: nvidia_usb_image_builder/__init__.py

    Re-exports NvidiaUsbImageBuilder so `from nvidia_usb_image_builder
    import NvidiaUsbImageBuilder` (as used by steam_os_utils.py) keeps
    working unchanged now that this is a package directory
    (nvidia_usb_image_builder/, holding nvidia_usb_image_builder.py,
    repatch_script.py, update_wrapper_script_builder.py, and
    install_to_hd.sh) instead of a single flat module.
'''

"""Package init: re-exports NvidiaUsbImageBuilder as the package's public API."""

from .nvidia_usb_image_builder import NvidiaUsbImageBuilder

__all__ = ['NvidiaUsbImageBuilder']
