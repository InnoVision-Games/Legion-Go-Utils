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

    file: AcpiEnabler.py
'''

import sys

from DkmsSupportedVersions import get_os_version
from DkmsSupportedVersions import get_kernel_modules_filename
from DkmsSupportedVersions import get_kernel_headers_filename
from FileDownloader import check_mirror_and_download_package
from ShellUtils import run_command

def prep_steamos(dry_run=True):
    print('\nDisabling SteamOS read-only mode.')
    command = ['sudo', 'steamos-readonly', 'disable']
    run_command(command, dry_run)

    print('\nUpdating packages.')
    command = ['sudo', 'pacman', '-Sy']
    run_command(command, dry_run)

    print('\nClearing Pacman keys')
    command = ['sudo', 'rm', '-r', '/etc/pacman.d/gnupg']
    run_command(command, dry_run)

    print('\nInitializing Pacman keys')
    command = ['sudo', 'pacman-key', '--init']
    run_command(command, dry_run)

    print('\nPopulating Pacman Arch Linux keys')
    command = ['sudo', 'pacman-key', '--populate', 'archlinux']
    run_command(command, dry_run)

    print('\nPopulating Pacman SteamOS holo keys')
    command = ['sudo', 'pacman-key', '--populate', 'holo']
    run_command(command, dry_run)

def install_kernel_packages(kernel_modules_filename, kernel_headers_filename, dry_run=True):
    print('\nInstalling required kernel modules')
    command = ['sudo', 'pacman', '-U', kernel_modules_filename, '--noconfirm']
    run_command(command, dry_run)

    print('\nInstalling required kernel headers')
    command = ['sudo', 'pacman', '-U', kernel_headers_filename, '--noconfirm']
    run_command(command, dry_run)

def finalize_steamos(dry_run=True):
    print('\nUpdating packages.')
    command = ['sudo', 'pacman', '-Sy']
    run_command(command, dry_run)

    print('\nInstalling Plymouth')
    command = ['sudo', 'pacman', '-Sy', 'plymouth', '--noconfirm']
    run_command(command, dry_run)

    print('\nInstalling Plymouth developer tools')
    command = ['sudo', 'pacman', '-Sy', 'dkms', 'git', 'base-devel', 'plymouth', '--noconfirm']
    run_command(command, dry_run)

    print('\nEnabling Linux Dynamic Kernel Module Support ACPI calls')
    command = ['sudo', 'pacman', '-Sy', 'acpi_call-dkms', '--noconfirm']
    run_command(command, dry_run)

    print('\nRe-enabling SteamOS read-only mode.')
    command = ['sudo', 'steamos-readonly', 'enable']
    run_command(command, dry_run)

def cleanup(kernel_modules_filename, kernel_headers_filename, dry_run=True):
    print('\nCleaning up: %s.' % kernel_headers_filename)
    run_command(['rm', kernel_modules_filename], dry_run)

    print('Cleaning up: %s.' % kernel_headers_filename)
    run_command(['rm', kernel_headers_filename], dry_run)

def check_dkms_acpi_calls_enabled(dry_run):
    result = run_command(['lsmod'])
    if 'acpi_call' in result.stdout:
        print('Linux DKMS needed for ACPI calls ARE enabled.')
        return True

    #print('Linux DKMS needed for ACPI calls are NOT enabled.')
    return False

def enable_acpi_calls(dry_run):
    os_version = get_os_version()
    kernel_modules_filename = get_kernel_modules_filename(os_version)
    kernel_headers_filename = get_kernel_headers_filename(os_version)

    # 1. Download the require kernel packages
    if not check_mirror_and_download_package(kernel_modules_filename):
        print('The  SteamOS requested is not supported yet, please try again later or open a support ticket with Valve')
        sys.exit(-1)
    if not check_mirror_and_download_package(kernel_headers_filename):
        print('\nError kernel module package: %s not found on Valve\'s mirror.' % kernel_headers_filename)
        print('Try again later or report an issue')
        sys.exit(-1)

    print('\nNow enabling ACPI calls on your current SteamOS installation')

    # 2. Prepare SteamOS and package manager for the installation
    prep_steamos(dry_run)

    # 3. Go ahead and install the required packages
    install_kernel_packages(kernel_modules_filename, kernel_headers_filename, dry_run)

    # 4. Enable the required services
    finalize_steamos(dry_run)

    # 5. Cleanup
    cleanup(kernel_modules_filename, kernel_headers_filename, dry_run)

    print('\nCongratulation! You now can enable custom fan curves and control charge limit!\n')
