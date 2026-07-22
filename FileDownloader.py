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

    file: FileDownloader.py
'''

import os
import shutil
import socket
import sys
import time
import urllib.request

# Set the global default timeout for all socket operations to 10 seconds
socket.setdefaulttimeout(10)

# URL containing Valve's public mirror for SteamOS packages
VALVE_PUBLIC_MIRROR = 'https://steamdeck-packages.steamos.cloud/archlinux-mirror/jupiter-main/os/x86_64/'

# Function that checks for and downloads a specified file.
def check_mirror_and_download_package(filename):
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
