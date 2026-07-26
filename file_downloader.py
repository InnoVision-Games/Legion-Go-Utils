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

    file: file_downloader.py
'''

"""Legacy single-mirror package downloader.

Orphaned: nothing in this project imports this module anymore. It has
been superseded by package_downloader.PackageDownloader, which adds
atomic (download-to-.part-then-rename) writes and local caching that this
module never had, and which both acpi_enabler.py and
nvidia_usb_image_builder.py now use instead. Kept around for reference /
historical continuity rather than deleted.
"""

import os
import shutil
import socket
import urllib.request

socket.setdefaulttimeout(10)

VALVE_PUBLIC_MIRROR = 'https://steamdeck-packages.steamos.cloud/archlinux-mirror/jupiter-main/os/x86_64/'


def check_mirror_and_download_package(filename):
    """Download filename from Valve's public mirror into the current directory.

    Args:
        filename: The package filename to fetch, relative to
            VALVE_PUBLIC_MIRROR.

    Returns:
        True if the download succeeded, False otherwise (the error is
        printed, not raised).
    """
    print('\nChecking Valve mirror for package: %s ...' % filename)
    try:
        remote_filename = os.path.join(VALVE_PUBLIC_MIRROR, filename)
        req = urllib.request.Request(url=remote_filename)
        with urllib.request.urlopen(req) as response:
            with open(filename, 'wb') as f:
                shutil.copyfileobj(response, f)
        print('File: %s, was downloaded successfully' % filename)
        return True
    except Exception as e:
        print('Error, file: %s, not found on Valve\'s mirror, with error:  %s' % (filename, str(e)))
        return False
