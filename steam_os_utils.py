#!/usr/bin/env python3

'''
    MIT License

    Copyright (c) 2025 InnoVision Games

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

    file: steam_os_utils.py
'''

"""Command-line entry point for the SteamOS-Utils toolset.

Parses CLI flags and dispatches to the ACPI enabler, the Legion Go 2
brightness-slider fix, and the NVIDIA USB installer image builder.
"""

import argparse
import sys

from acpi_enabler import AcpiEnabler
from legion import enable_lego2_brightness_slider
from legion import remove_lego2_brightness_slider
from nvidia_usb_image_builder import NvidiaUsbImageBuilder

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='A set of tools for devices running SteamOS')
    parser.add_argument('-acpi', '--enable_acpi_calls', action='store_true', help='Enable Linux Dynamic Kernel Module Support ACPI calls')
    parser.add_argument('-removeacpi', '--disable_acpi_calls', action='store_true', help='Disable and remove Linux Dynamic Kernel Module Support ACPI calls (and the shared update wrapper, if acpi_call was the only self-heal payload configured)')
    parser.add_argument('-lego2brightness', '--enable_lego2_brightness_slider', action='store_true', help='Enable Legion Go 2 Brightness Slider and Color correction Fix')
    parser.add_argument('-removelego2brightness', '--remove_lego2_brightness_slider', action='store_true', help='Disable and remove Legion Go 2 Brightness Slider and Color Correction Fix')

    # --- NVIDIA USB installer image builder (nvidia_usb_image_builder) ---
    nvidia_group = parser.add_argument_group('NVIDIA USB installer image builder')
    nvidia_group.add_argument('-build_nvidia_usb', '--build_nvidia_usb_image', action='store_true',
                               help='Build a one-click NVIDIA USB installer image from a clean SteamOS OOBE repair image')
    nvidia_group.add_argument('nvidia_image', type=str, nargs='?', default=None,
                               help='Path to the clean SteamOS OOBE repair .img to patch '
                                    '(positional; auto-detected next to the script if omitted)')
    nvidia_group.add_argument('-nvidia_output', '--nvidia_output_path', type=str, default=None,
                               help='Path for the built installer image (default: the input image\'s filename with "-nvidia" appended)')
    nvidia_group.add_argument('-nvidia_driver', '--nvidia_driver_spec', type=str, default='latest',
                               help="NVIDIA driver to install: 'latest' (default) or an Arch version prefix, e.g. 580 / 580.105.08")
    nvidia_group.add_argument('-nvidia_update_mode', '--nvidia_update_mode', type=str, default='selfheal',
                               choices=['selfheal', 'hold', 'stock'],
                               help='How OS updates interact with the driver: selfheal (default), hold, or stock')
    nvidia_group.add_argument('-nvidia_no_installer', '--nvidia_no_installer', action='store_true',
                               help='Skip adding the one-click USB installer (produce a plain patched OS image)')
    nvidia_group.add_argument('-nvidia_trim_cuda', '--nvidia_trim_cuda', action='store_true',
                               help='Drop CUDA/OpenCL/NVVM/OptiX libraries to shrink the image (~350 MB smaller)')
    nvidia_group.add_argument('-nvidia_skip_sigcheck', '--nvidia_skip_sigcheck', action='store_true',
                               help='Disable pacman signature checks in the build chroot')
    nvidia_group.add_argument('-nvidia_workdir', '--nvidia_workdir', type=str, default=None,
                               help='Build working directory (default: alongside the output image; cached between runs)')
    nvidia_group.add_argument('-nvidia_verbose', '--nvidia_verbose', action='store_true',
                               help='Print each underlying shell command as it runs')

    args = parser.parse_args()

    if args.enable_acpi_calls:
        acpi_enabler = AcpiEnabler()
        try:
            acpi_enabler.enable()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
    if args.disable_acpi_calls:
        acpi_enabler = AcpiEnabler()
        try:
            acpi_enabler.disable()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
    if args.enable_lego2_brightness_slider:
        enable_lego2_brightness_slider()
    if args.remove_lego2_brightness_slider:
        remove_lego2_brightness_slider()
    if args.build_nvidia_usb_image:
        builder = NvidiaUsbImageBuilder(
            image_path=args.nvidia_image,
            output_path=args.nvidia_output_path,
            driver_spec=args.nvidia_driver_spec,
            update_mode=args.nvidia_update_mode,
            add_installer=not args.nvidia_no_installer,
            trim_cuda=args.nvidia_trim_cuda,
            skip_sigcheck=args.nvidia_skip_sigcheck,
            workdir=args.nvidia_workdir,
            verbose=args.nvidia_verbose,
        )
        try:
            builder.build()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
