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

    file: dkms_supported_versions.py
'''

"""Helpers for identifying the running SteamOS/neptune kernel version.

Used by acpi_enabler.py to work out the exact linux-neptune kernel
modules/headers package filenames matching the currently running kernel.

get_remote_kernel_modules_path()/get_remote_kernel_headers_path() used to
live here, building a URL against a single hardcoded Valve mirror (the
now-removed file_downloader.py module's VALVE_PUBLIC_MIRROR). Removed:
AcpiEnabler now resolves its download URLs itself, from the running
system's own pacman.conf / mirrorlist (see
AcpiEnabler._resolve_kernel_package_url()), the same approach
NvidiaUsbImageBuilder.resolve_headers_url() uses against a mounted
image's pacman.conf/mirrorlist — not a single hardcoded URL.
"""

import platform


def get_os_version():
    """Parse the running kernel's release string into its component parts.

    Returns:
        A dict with keys os_name, kernel_type, kernel_short_version,
        kernel_long_version, vendor_version, and sub_version, derived from
        platform.release() (e.g. "6.11.0-valve10-1-neptune").
    """
    temp = platform.release()
    temp = temp.split('-')

    os_version = {
        'os_name': 'linux',
        'kernel_type': 'neptune',
        'kernel_short_version': str(temp[4]) if len(temp) > 4 else '0',
        'kernel_long_version': str(temp[0]),
        'vendor_version': str(temp[1]) if len(temp) > 1 else '0',
        'sub_version': str(temp[2]) if len(temp) > 2 else '0',
    }
    return os_version


def get_kernel_modules_filename(os_version):
    """Build the linux-neptune kernel modules package filename.

    Args:
        os_version: A dict as returned by get_os_version().

    Returns:
        The expected pacman package filename for the kernel modules
        package matching os_version.
    """
    return ''.join([
        os_version['os_name'] + '-',
        os_version['kernel_type'] + '-',
        os_version['kernel_short_version'] + '-',
        os_version['kernel_long_version'] + '.',
        os_version['vendor_version'] + '-',
        os_version['sub_version'] + '-',
        'x86_64.pkg.tar.zst',
    ])


def get_kernel_headers_filename(os_version):
    """Build the linux-neptune kernel headers package filename.

    Args:
        os_version: A dict as returned by get_os_version().

    Returns:
        The expected pacman package filename for the kernel headers
        package matching os_version.
    """
    return ''.join([
        os_version['os_name'] + '-',
        os_version['kernel_type'] + '-',
        os_version['kernel_short_version'] + '-headers-',
        os_version['kernel_long_version'] + '.',
        os_version['vendor_version'] + '-',
        os_version['sub_version'] + '-',
        'x86_64.pkg.tar.zst',
    ])
