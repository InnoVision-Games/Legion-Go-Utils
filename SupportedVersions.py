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

    file: SupportedVersions.py
'''

import os
import platform
import sys

os_remote_kernel_modules_path = {
    'linux-neptune-611-6.11.11.valve24-2-x86_64.pkg.tar.zst' : 'jupiter-3.7/os/x86_64/',
    'linux-neptune-611-6.11.11.valve26-1-x86_64.pkg.tar.zst' : 'jupiter-3.7/os/x86_64/',
}

os_remote_kernel_headers_path = {
    'linux-neptune-611-headers-6.11.11.valve24-2-x86_64.pkg.tar.zst' : 'jupiter-3.7/os/x86_64/',
    'linux-neptune-611-headers-6.11.11.valve26-1-x86_64.pkg.tar.zst' : 'jupiter-3.7/os/x86_64/',
}

kernel_modules_packages = [
    'linux-neptune-611-6.11.11.valve24-2-x86_64.pkg.tar.zst',
    'linux-neptune-611-6.11.11.valve26-1-x86_64.pkg.tar.zst',
]

kernel_headers_packages = [
    'linux-neptune-611-headers-6.11.11.valve24-2-x86_64.pkg.tar.zst',
    'linux-neptune-611-headers-6.11.11.valve26-1-x86_64.pkg.tar.zst',
]

dkms_acpi_enabled_strings = [
    'acpi_call/1.2.2, 6.11.11-valve24-2-neptune-611-gfd0dd251480d, x86_64: installed',
]

def get_os_version():
    temp = platform.release();
    temp = temp.split('-')

    os_version = {
        'os_name' : 'linux',
        'kernel_type' : 'neptune',
        'kernel_short_version' : str(temp[4]),
        'kernel_long_version' : str(temp[0]),
        'vendor_version' : str(temp[1]),
        'sub_version' : str(temp[2])
    }

    print('\nos_version: ')
    print(os_version)

    return os_version

def get_kernel_modules_filename(os_version):
    print('\nNow generating kernel modules filename...')

    kernel_modules_filename = [
        os_version['os_name'] + '-',
        os_version['kernel_type'] + '-',
        os_version['kernel_short_version'] + '-',
        os_version['kernel_long_version'] + '.',
        os_version['vendor_version'] + '-',
        os_version['sub_version'] + '-',
        'x86_64.pkg.tar.zst'
    ]
    seperator = ''
    kernel_modules_filename = seperator.join(kernel_modules_filename)

    print('Generated kernel modules filename: %s.' % kernel_modules_filename)

    return kernel_modules_filename

def get_kernel_headers_filename(os_version):
    print('\nNow generating kernel headers filename...')

    kernel_headers_filename = [
        os_version['os_name'] + '-',
        os_version['kernel_type'] + '-',
        os_version['kernel_short_version'] + '-headers-',
        os_version['kernel_long_version'] + '.',
        os_version['vendor_version'] + '-',
        os_version['sub_version'] + '-',
        'x86_64.pkg.tar.zst'
    ]
    seperator = ''
    kernel_headers_filename = seperator.join(kernel_headers_filename)

    print('Generated kernel headers filename: %s.' % kernel_headers_filename)

    return kernel_headers_filename

def get_remote_kernel_modules_path(kernel_modules_filename):
    print('\nNow generating kernel_modules_filename: %s remote relative path...' % kernel_modules_filename)
    remote_file_path = ''
    try:
        remote_file_path = os_remote_kernel_modules_path[kernel_modules_filename]
    except KeyError:
        print('\nError kernel_modules_filename: %s not found in lookup table!' % kernel_modules_filename)
        sys.exit(-1)

    remote_filename = os.path.join(remote_file_path, kernel_modules_filename)

    print('Generated remote filename: %s.' % remote_filename)

    return remote_filename

def get_remote_kernel_headers_path(kernel_headers_filename):
    print('\nNow generating kernel_headers_filename: %s remote relative path...' % kernel_headers_filename)
    remote_file_path = ''
    try:
        remote_file_path = os_remote_kernel_headers_path[kernel_headers_filename]
    except KeyError:
        print('\nError filename: %s not found in lookup table!' % kernel_headers_filename)
        sys.exit(-1)

    remote_filename = os.path.join(remote_file_path, kernel_headers_filename)

    print('Generated remote filename: %s.' % remote_filename)

    return remote_filename
