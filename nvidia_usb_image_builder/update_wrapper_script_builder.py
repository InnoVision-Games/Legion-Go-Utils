#!/usr/bin/env python3

'''
    MIT License

    Copyright (c) 2026 InnoVision Games

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

    file: update_wrapper_script_builder.py
'''

"""Generator for the on-device update wrapper source (selfheal mode)."""


class UpdateWrapperScriptBuilder:
    """Generates the source of the on-device update wrapper(s).

    NvidiaUsbImageBuilder installs the rendered wrapper(s) in 'selfheal'
    update mode. Each generated script REPLACES a real Valve binary in
    /usr/bin (its original is preserved alongside it as <name>.orig): it
    runs the real updater first, then — if an update was actually
    applied — runs repatch.py (see repatch_script.py) to rebuild the
    NVIDIA driver in the freshly staged OS slot. If the repatch fails,
    the update is cancelled at the bootloader level so the bootloader
    keeps booting the current, still-working image rather than trying an
    unpatched one.

    Pulled out of NvidiaUsbImageBuilder into its own class for the same
    reason repatch_script.py lives in its own file: the generated
    on-device script is a distinct, independently-testable unit from the
    build-host orchestration logic in NvidiaUsbImageBuilder. Unlike
    repatch.py, this one genuinely is rendered per-call — render() takes
    a binary_name, since the same wrapper source gets installed under
    more than one on-device filename — so it stays a generator class
    rather than becoming a static file.

    render() takes a binary_name because an OS update can be triggered
    through more than one entry point Valve ships — the desktop-mode
    `steamos-update` CLI, `steamos-update-os`, or Game Mode's
    `steamos-atomupd-client`. An EARLIER version of this project only
    wrapped `steamos-update`, which meant an update triggered via one of
    the other two (confirmed: switching update BRANCHES, e.g.
    stable -> main, on real hardware) never ran the self-heal machinery
    at all — the newly staged slot got no NVIDIA driver and no safety
    net, and the system couldn't reach the UI. NvidiaUsbImageBuilder now
    calls render() once per entry point that actually exists in the
    image, each wrapping its own real binary; repatch.py is idempotent,
    so it's harmless if more than one ends up invoking it for the same
    underlying update.

    edit_other_confs() (inside the generated script) restores the safety
    net an EARLIER version of this project dropped entirely: on a genuine
    repatch failure it directly edits the OTHER slot's boot conf on the
    ESP (zeroing boot-requested-at/boot-attempts, marking image-invalid)
    since steamos-bootconf set-mode alone does not reliably undo a staged
    switch. `sed -i` is replaced with `re.sub()` over the conf text per
    the project's "use Python everywhere possible" goal.

    Attributes:
        repatch_path: On-device path to repatch.py the rendered wrapper
            invokes.
        log_path: On-device path the rendered wrapper logs repatch
            output to.
    """

    DEFAULT_REPATCH_PATH = '/usr/lib/steamos-nvidia/repatch.py'
    DEFAULT_LOG_PATH = '/var/log/steamos-nvidia-repatch.log'

    def __init__(self, repatch_path=None, log_path=None):
        """Initialize the builder.

        Args:
            repatch_path: On-device path to repatch.py. Defaults to
                DEFAULT_REPATCH_PATH.
            log_path: On-device path the wrapper logs repatch output to.
                Defaults to DEFAULT_LOG_PATH.
        """
        self.repatch_path = repatch_path or self.DEFAULT_REPATCH_PATH
        self.log_path = log_path or self.DEFAULT_LOG_PATH

    def render(self, binary_name='steamos-update'):
        """Render the wrapper source for a single real binary.

        binary_name selects which one this particular copy wraps
        (steamos-update / steamos-update-os / steamos-atomupd-client) —
        each wraps its own preserved <binary_name>.orig, so whichever
        entry point the OS actually invokes for a given update runs the
        same real-update-then-repatch logic. This REPLACES a real binary
        on-device, so it must be standalone and dependency-free —
        SteamOS ships python3, so that's satisfied the same way
        repatch.py is.

        Args:
            binary_name: The real on-device binary this copy wraps.

        Returns:
            The full Python source of the wrapper script, as a string.
        """
        real_path = '/usr/bin/%s.orig' % binary_name
        return (
            '#!/usr/bin/env python3\n'
            '"""\n'
            + binary_name + ' wrapper (steamos-nvidia self-healing updates).\n'
            'Runs Valve\'s real updater, then rebuilds the NVIDIA driver inside the\n'
            'freshly staged OS slot. If that fails, the update is cancelled: the\n'
            'bootloader keeps booting the current (working) image.\n'
            '"""\n'
            'import re\n'
            'import subprocess\n'
            'import sys\n'
            'from pathlib import Path\n'
            '\n'
            'REAL = ' + repr(real_path) + '\n'
            'REPATCH = ' + repr(self.repatch_path) + '\n'
            'LOG = Path(' + repr(self.log_path) + ')\n'
            '\n'
            '\n'
            'def edit_other_confs(edits):\n'
            '    """Edit the boot config of every slot EXCEPT the current one.\n'
            '\n'
            '    The conf files on the ESP are plain text; editing them directly is\n'
            '    the only revert that reliably steers steamcl (set-mode booted does\n'
            '    NOT undo a staged switch, and a zeroed boot-requested-at still gets\n'
            '    retried while boot-attempts is nonzero -- both verified the hard way\n'
            '    by the original bash tool\'s author).\n'
            '\n'
            '    Args:\n'
            '        edits: List of (pattern, replacement) regex pairs, applied per\n'
            '            conf.\n'
            '    """\n'
            '    try:\n'
            '        this_image = subprocess.run(\n'
            '            [\'steamos-bootconf\', \'this-image\'],\n'
            '            capture_output=True, text=True,\n'
            '        ).stdout.strip()\n'
            '    except (OSError, subprocess.SubprocessError):\n'
            '        return\n'
            '    if not this_image:\n'
            '        return\n'
            '    conf_dir = Path(\'/esp/SteamOS/conf\')\n'
            '    if not conf_dir.is_dir():\n'
            '        return\n'
            '    for conf in conf_dir.glob(\'*.conf\'):\n'
            '        if conf.stem == this_image:\n'
            '            continue\n'
            '        text = conf.read_text()\n'
            '        for pattern, replacement in edits:\n'
            '            text = re.sub(pattern, replacement, text, flags=re.M)\n'
            '        conf.write_text(text)\n'
            '    subprocess.run([\'sync\', \'-f\', str(conf_dir)], check=False)\n'
            '\n'
            '\n'
            'def main():\n'
            '    is_apply = not any(a in (\'check\', \'--supports-duplicate-detection\') for a in sys.argv[1:])\n'
            '\n'
            '    rc = subprocess.run([REAL] + sys.argv[1:]).returncode\n'
            '\n'
            '    if rc == 0 and is_apply:\n'
            '        print(\'Update staged. Building NVIDIA driver for the new OS \'\n'
            '              \'(10-20 min, do NOT power off)...\', file=sys.stderr)\n'
            '        with LOG.open(\'ab\') as log_fh:\n'
            '            result = subprocess.run([\'python3\', REPATCH, \'other\'], stdout=log_fh,\n'
            '                                     stderr=subprocess.STDOUT)\n'
            '        if result.returncode == 0:\n'
            '            print(\'NVIDIA driver installed into the updated OS. Safe to reboot.\', file=sys.stderr)\n'
            '            # make sure the freshly patched slot is bootable (clears an\n'
            '            # image-invalid left by a previously cancelled update)\n'
            '            edit_other_confs([(r\'^image-invalid:.*\', \'image-invalid: 0\')])\n'
            '        else:\n'
            '            print(\'!! NVIDIA driver rebuild FAILED -- cancelling this update.\', file=sys.stderr)\n'
            '            print(\'!! The system will keep booting the current working version.\', file=sys.stderr)\n'
            '            print(\'!! Details: %s\' % LOG, file=sys.stderr)\n'
            '            edit_other_confs([\n'
            '                (r\'^boot-requested-at:.*\', \'boot-requested-at: 0\'),\n'
            '                (r\'^boot-attempts:.*\', \'boot-attempts: 0\'),\n'
            '                (r\'^image-invalid:.*\', \'image-invalid: 1\'),\n'
            '            ])\n'
            '            subprocess.run([\'steamos-bootconf\', \'set-mode\', \'booted\'], check=False)\n'
            '            sys.exit(1)\n'
            '\n'
            '    sys.exit(rc)\n'
            '\n'
            '\n'
            'if __name__ == \'__main__\':\n'
            '    main()\n'
        )
