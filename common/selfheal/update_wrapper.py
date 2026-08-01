#!/usr/bin/env python3

# MIT License
#
# Copyright (c) 2026 InnoVision Games
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# file: update_wrapper.py
#
# steamos-utils update wrapper -- REPLACES a real Valve update binary in
# /usr/bin (its original is preserved alongside it as <name>.orig): runs
# the real updater first, then -- if an update was actually applied --
# runs repatch.py (see repatch_script.py) to rebuild this device's
# configured self-heal payload(s) in the freshly staged OS slot. If the
# repatch fails, the update is cancelled at the bootloader level so it
# keeps booting the current, still-working image rather than trying an
# unpatched one.
#
# Installed under more than one on-device filename -- steamos-update
# always, steamos-update-os and steamos-atomupd-client if present -- an
# update triggered through any one of Valve's entry points must not skip
# the self-heal machinery (confirmed the hard way: switching update
# BRANCHES, e.g. stable -> main, on real hardware goes through
# steamos-atomupd-client, not steamos-update). Every installed copy is
# byte-identical; WHICH real binary a given copy wraps is recovered at
# runtime from its own invoked name (sys.argv[0]) rather than baked in
# per-copy, the same way repatch.py takes its target partition set as a
# runtime argument instead of being regenerated per-partset -- nothing
# here actually needs per-install templating. This mirrors why
# repatch_script.py and install_to_hd.sh are real standalone scripts
# too: NvidiaUsbImageBuilder.install_one_click_installer() and
# _configure_selfheal_updates() / AcpiEnabler._configure_selfheal_updates()
# just copy this file into place under each wrapped name verbatim.
#
# edit_other_confs() restores the safety net an EARLIER version of this
# project dropped entirely: on a genuine repatch failure it directly
# edits the OTHER slot's boot conf on the ESP (zeroing
# boot-requested-at/boot-attempts, marking image-invalid) since
# steamos-bootconf set-mode alone does not reliably undo a staged
# switch. `sed -i` is replaced with `re.sub()` over the conf text per
# the project's "use Python everywhere possible" goal.

"""
steamos-utils update wrapper -- runs Valve's real updater, then rebuilds
this device's configured self-heal payload(s) inside the freshly staged
OS slot. If that fails, the update is cancelled: the bootloader keeps
booting the current (working) image. Installed under more than one
on-device filename; recovers which real binary it wraps from its own
invoked name at runtime.
"""

import re
import subprocess
import sys
from pathlib import Path

REPATCH = '/usr/lib/steamos-utils/repatch.py'
LOG = Path('/var/log/steamos-utils-repatch.log')

# Recovered from our own invoked name rather than baked in per-install --
# see the module comment above for why nothing here needs per-copy
# templating. Path(...).name strips any directory component in case
# we're ever invoked by full path rather than bare name.
BINARY_NAME = Path(sys.argv[0]).name
REAL = '/usr/bin/%s.orig' % BINARY_NAME


def edit_other_confs(edits):
    """Edit the boot config of every slot EXCEPT the current one.

    The conf files on the ESP are plain text; editing them directly is
    the only revert that reliably steers steamcl (set-mode booted does
    NOT undo a staged switch, and a zeroed boot-requested-at still gets
    retried while boot-attempts is nonzero -- both verified the hard way
    by the original bash tool's author).

    Args:
        edits: List of (pattern, replacement) regex pairs, applied per
            conf.
    """
    try:
        this_image = subprocess.run(
            ['steamos-bootconf', 'this-image'],
            capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if not this_image:
        return
    conf_dir = Path('/esp/SteamOS/conf')
    if not conf_dir.is_dir():
        return
    for conf in conf_dir.glob('*.conf'):
        if conf.stem == this_image:
            continue
        text = conf.read_text()
        for pattern, replacement in edits:
            text = re.sub(pattern, replacement, text, flags=re.M)
        conf.write_text(text)
    subprocess.run(['sync', '-f', str(conf_dir)], check=False)


def main():
    is_apply = not any(a in ('check', '--supports-duplicate-detection') for a in sys.argv[1:])

    rc = subprocess.run([REAL] + sys.argv[1:]).returncode

    if rc == 0 and is_apply:
        print('Update staged. Rebuilding self-heal payload(s) (NVIDIA driver '
              'and/or acpi_call, whichever are configured) for the new OS '
              '(10-20 min, do NOT power off)...', file=sys.stderr)
        with LOG.open('ab') as log_fh:
            result = subprocess.run(['python3', REPATCH, 'other'], stdout=log_fh,
                                     stderr=subprocess.STDOUT)
        if result.returncode == 0:
            print('Self-heal payload(s) installed into the updated OS. Safe to reboot.', file=sys.stderr)
            # make sure the freshly patched slot is bootable (clears an
            # image-invalid left by a previously cancelled update)
            edit_other_confs([(r'^image-invalid:.*', 'image-invalid: 0')])
        else:
            print('!! Self-heal rebuild FAILED -- cancelling this update.', file=sys.stderr)
            print('!! The system will keep booting the current working version.', file=sys.stderr)
            print('!! Details: %s' % LOG, file=sys.stderr)
            edit_other_confs([
                (r'^boot-requested-at:.*', 'boot-requested-at: 0'),
                (r'^boot-attempts:.*', 'boot-attempts: 0'),
                (r'^image-invalid:.*', 'image-invalid: 1'),
            ])
            subprocess.run(['steamos-bootconf', 'set-mode', 'booted'], check=False)
            sys.exit(1)

    sys.exit(rc)


if __name__ == '__main__':
    main()
