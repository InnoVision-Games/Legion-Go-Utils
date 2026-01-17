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

from ShellUtils import run_command

def download_kernel_packages(kernel_modules_filename, kernel_headers_filename, dry_run=True):
    steam_packages_url = 'https://steamdeck-packages.steamos.cloud/archlinux-mirror/'
    kernel_modules_url = steam_packages_url + kernel_modules_filename
    kernel_headers_url = steam_packages_url + kernel_headers_filename

    print('\nDownloading kernel modules and header packages.')
    run_command(['wget', kernel_modules_url], dry_run)
    run_command(['wget', kernel_headers_url], dry_run)
