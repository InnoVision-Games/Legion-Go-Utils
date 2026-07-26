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

    file: shell_utils.py
'''

"""Shared shell command runner used by the simpler tools in this project.

Not used by nvidia_usb_image_builder.py, which shells out constantly and
needs quieter, more granular control over what gets printed — see the
note near that module's own _run()/_run_quiet() helpers for why.
"""

import shlex
import subprocess


def run_command(command, verbose=False):
    """Run a shell command and print its stdout/stderr.

    Only prints stdout/stderr when they actually have content, and
    without adding an extra trailing blank line on top of whatever the
    command itself already printed -- most commands run by this
    project's simpler tools (mkdir, rm -rf, mount, truncate, ...)
    produce no output at all on success, and printing two blank lines
    per call for those turns routine, mostly-silent runs into a wall of
    blank lines with no real content in it.

    Args:
        command: List of command argv parts (joined via shlex.join and
            executed through the shell).
        verbose: If True, print the command line before running it.

    Returns:
        The completed subprocess.CompletedProcess on success, or None if
        the command failed (the error is printed, not raised).
    """
    result = None
    try:
        command = shlex.join(command)
        if verbose:
            print('Running command: %s.' % command)
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            check=True,
            universal_newlines=True,
        )
        if result.stdout:
            print(result.stdout, end='')
        if result.stderr:
            print(result.stderr, end='')
    except subprocess.CalledProcessError as e:
        print('Shell command failed with error: %s' % e)
        if e.stderr:
            print('Stderr: %s' % e.stderr, end='')

    return result
