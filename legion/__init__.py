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

    file: legion/__init__.py

    Re-exports the Legion Go 2 brightness slider fix functions so
    `from legion import enable_lego2_brightness_slider,
    remove_lego2_brightness_slider` (as used by steam_os_utils.py) has a
    stable import path. Named "legion" rather than mirroring the module
    filename (unlike acpi_enabler/, nvidia_usb_image_builder/) since this
    package is meant to hold Legion-hardware-specific fixes generally,
    not just this one brightness-slider stub -- future Legion-family
    quirks can live alongside it here without another top-level rename.
'''

"""Package init: re-exports the Legion Go 2 brightness slider fix functions."""

from .legion_go2_brightness_slider import enable_lego2_brightness_slider
from .legion_go2_brightness_slider import remove_lego2_brightness_slider

__all__ = ['enable_lego2_brightness_slider', 'remove_lego2_brightness_slider']
