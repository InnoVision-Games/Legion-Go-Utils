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

    file: LegionGoHelper.py
'''

import argparse
import sys

from AcpiEnabler import check_dkms_acpi_calls_enabled
from AcpiEnabler import enable_acpi_calls

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='A set of tools for Legion Go devices running SteamOS')
    parser.add_argument('-d', '--dry_run', action='store_true', help='Test the commands without executing them')
    parser.add_argument('-acpi', '--enable_acpi_calls', action='store_true', help='Enable Linux Dynamic Kernel Module Support ACPI calls')
    parser.add_argument('-check_acpi', '--check_dkms_acpi_calls_enabled', action='store_true', help='Check if Linux Dynamic Kernel Module Support ACPI calls are enabled')

    args = parser.parse_args()

    if args.enable_acpi_calls:
        enable_acpi_calls(args.dry_run)
    if args.check_dkms_acpi_calls_enabled:
        check_dkms_acpi_calls_enabled(args.dry_run)
