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
# file: repatch_script.py
#
# steamos-utils repatch -- rebuilds + installs this device's configured
# self-heal payload(s) into another partition set (normally "other"),
# right after an OS update staged it there. Covers the NVIDIA driver
# and/or the acpi_call DKMS module, driven independently by whichever of
# driver.json/acpi_call.json actually exist -- see main()'s docstring.
# Installed onto the target SteamOS device at
# /usr/lib/steamos-utils/repatch.py by
# NvidiaUsbImageBuilder._configure_selfheal_updates() and/or
# AcpiEnabler._configure_selfheal_updates() (whichever ran first; both
# install the identical file), and run unattended by the installed
# update_wrapper.py copies (see that file). Run as root; each
# payload is independently idempotent (skipped if the target slot already
# has it for its kernel); logs to stdout, which the wrapper redirects to
# a log file.
#
# Shipped as a real standalone script (matching install_to_hd.sh) instead
# of a Python string generator: CMDLINE_ADD, BUILD_ONLY_RE, and
# DRIVER_CONF_PATH below are the same fixed values
# NvidiaUsbImageBuilder always builds with (its own CMDLINE_ADD /
# BUILD_ONLY_RE / the default /usr/lib/steamos-utils/driver.json path),
# so there was nothing actually being templated at build time --
# NvidiaUsbImageBuilder.install_one_click_installer()-style, this file
# just gets copied into the image verbatim by
# _configure_selfheal_updates() instead of being rendered from a class.
#
# Two safety fixes, both confirmed necessary the hard way against real
# hardware, live in this script:
#
# - Process killing during cleanup NEVER uses `fuser -km`. MERGED/dev,
#   MERGED/sys, NEWROOT/dev, NEWROOT/sys are --rbind mounts of the REAL
#   host /dev and /sys -- the exact same inodes, not copies. fuser
#   matches by open file descriptor against inode identity, so
#   `fuser -km` against a path containing those bind-mounts can match
#   (and kill) ANY process on the machine that simply has /dev/null,
#   /dev/urandom, or a tty open -- effectively everything. This matters
#   even more here than in the image-build tool, since this script runs
#   unattended on a live, in-use SteamOS install during a real update.
#   Only processes actually chrooted INTO a given path (matched via
#   /proc/<pid>/root) are ever killed.
#
# - Every chroot invocation drops CAP_SYS_MODULE / CAP_SYS_BOOT /
#   CAP_SYS_RAWIO from its capability bounding set via setpriv. chroot()
#   only changes the filesystem root, not the kernel/module/device
#   namespace -- a process inside the chroot that calls
#   modprobe/insmod/rmmod, or reboot(2), is still talking to the one
#   real running kernel. A dkms/pacman post-install hook doing that
#   unprompted (a driver package sanity-loading the module it just
#   built) has been confirmed to crash a live desktop session running
#   the build tool; the same risk applies here, unattended, on the
#   user's actual machine.

"""
steamos-utils repatch -- rebuild + install this device's configured
self-heal payload(s) (NVIDIA driver and/or acpi_call) into another
partition set (normally "other"), right after an OS update staged it
there. Run as root. Each payload is independently idempotent: skipped
if the slot already has it for its kernel. Logs to stdout (the update
wrapper redirects to a log file).
"""
import atexit
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PARTSET = sys.argv[1] if len(sys.argv) > 1 else 'other'

DRIVER_CONF_PATH = '/usr/lib/steamos-utils/driver.json'
CMDLINE_ADD = 'rd.driver.blacklist=nouveau modprobe.blacklist=nouveau nvidia-drm.modeset=1 nvidia-drm.fbdev=1'
BUILD_ONLY_RE = re.compile('^(dkms|nvidia-open-dkms|patch|gcc|gcc-libs|make|binutils|libisl|libmpc|mpfr|pahole|python-setuptools|linux-neptune.*-headers|.*-headers)$')

# acpi_call.json: written by AcpiEnabler._configure_selfheal_updates()
# the same way driver.json is written by
# NvidiaUsbImageBuilder._configure_selfheal_updates() -- lives right
# alongside it, in the same shared steamos-utils/ directory (see
# main()'s "propagate the self-healing machinery" step, which already
# copies that whole directory wholesale regardless of what's in it).
# Unlike the driver, there is nothing to pin here beyond the module name
# and its upstream repo -- acpi_call has no equivalent of the driver's
# exact pkg_urls, so self-heal just re-resolves whatever the latest
# release tag is, same as AcpiEnabler's own initial install.
ACPI_CALL_CONF_PATH = '/usr/lib/steamos-utils/acpi_call.json'


def resolve_acpi_call_version(repo):
    """Resolve the latest vX.Y.Z release tag of repo via git ls-remote.

    Same matching rule as AcpiEnabler._resolve_acpi_call_version(): only
    proper "vX.Y.Z" release tags count, since the repo also carries
    legacy tags named after a supported KERNEL version rather than a
    module release (e.g. "3.17"), which sort numerically above real
    releases as a bare tuple but aren't valid `git clone --branch` refs
    under a "v"-prefixed name.

    Args:
        repo: Git URL to list release tags of.

    Returns:
        A (version, tag) tuple, e.g. ('1.2.2', 'v1.2.2'), or (None,
        None) if the repo's tags couldn't be listed or none matched.
    """
    result = subprocess.run(['git', 'ls-remote', '--tags', '--refs', repo],
                             capture_output=True, text=True)
    best = None
    for line in (result.stdout or '').splitlines():
        m = re.search(r'refs/tags/(v[0-9]+(?:\.[0-9]+)*)$', line)
        if not m:
            continue
        tag = m.group(1)
        version = tag[1:]
        version_tuple = tuple(int(part) for part in version.split('.'))
        if best is None or version_tuple > best[0]:
            best = (version_tuple, version, tag)
    return (best[1], best[2]) if best else (None, None)


def log(msg):
    """Print an informational [repatch] log line to stdout.

    Args:
        msg: The message to log.
    """
    print('[repatch] %s' % msg, flush=True)


def die(msg):
    """Print a [repatch] failure line to stderr and exit with status 1.

    Args:
        msg: The failure message to log.
    """
    print('[repatch] FAIL: %s' % msg, file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd, **kw):
    """Run cmd, raising if it exits non-zero.

    Args:
        cmd: Argv list to execute.
        **kw: Extra keyword arguments forwarded to subprocess.run().

    Returns:
        The completed subprocess.CompletedProcess.
    """
    return subprocess.run(cmd, check=True, **kw)


def run_ok(cmd, **kw):
    """Run cmd and report whether it succeeded.

    Args:
        cmd: Argv list to execute.
        **kw: Extra keyword arguments forwarded to subprocess.run().

    Returns:
        True if cmd exited 0, False otherwise.
    """
    return subprocess.run(cmd, **kw).returncode == 0


def is_mountpoint(path):
    """Check whether path is currently a mountpoint.

    Args:
        path: Path to check.

    Returns:
        True if path is a mountpoint, False otherwise.
    """
    return subprocess.run(['mountpoint', '-q', str(path)]).returncode == 0


def kill_chrooted(target):
    """Kill only processes actually chrooted into target.

    Matches via /proc/<pid>/root, never via fuser. MERGED/dev,
    MERGED/sys, NEWROOT/dev, NEWROOT/sys are --rbind mounts of THIS
    MACHINE'S real /dev and /sys -- the exact same inodes, not copies.
    `fuser -km` against a path containing those bind-mounts matches (and
    kills) ANY process on this system that simply has /dev/null,
    /dev/urandom, or a tty open -- effectively everything -- which can
    crash the running system this script is patching. This matters even
    more here than in the image-build tool, since repatch.py runs
    unattended on a live, in-use SteamOS install during a real update.

    Args:
        target: Path whose chrooted processes should be killed.
    """
    try:
        target = os.path.realpath(str(target))
    except OSError:
        return
    for pid_dir in Path('/proc').glob('[0-9]*'):
        try:
            root = os.path.realpath(str(pid_dir / 'root'))
        except OSError:
            continue
        if root != target:
            continue
        try:
            os.kill(int(pid_dir.name), 9)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    time.sleep(0.2)


def quiet_umount(path):
    """Unmount path if it is currently mounted, killing chrooted holders first.

    Args:
        path: Path to unmount.
    """
    if not is_mountpoint(path):
        return
    kill_chrooted(path)
    time.sleep(0.2)
    if run_ok(['umount', '-R', str(path)]):
        return
    run_ok(['umount', '-Rl', str(path)])


# SteamOS /home is ext4 with casefold enabled, which overlayfs rejects
# as an upperdir -- so the build workspace lives inside a plain ext4
# loopback image on /home (space for the build, no casefold).
NEWROOT = Path(tempfile.mkdtemp(prefix='repatch-root-'))
WORK = Path(tempfile.mkdtemp(prefix='repatch-work-'))
WORKIMG = Path('/home/.steamos-utils-work.img')
UPPER = WORK / 'upper'
OVLWORK = WORK / 'ovlwork'
MERGED = WORK / 'merged'
state = {'was_ro': False, 'rootfs_type': None}


# chroot() only changes the filesystem root -- it does NOT give the
# build chroot its own kernel/module/device namespace. A process
# inside MERGED that calls modprobe/insmod/rmmod is still talking to
# THIS machine's real running kernel, and reboot(2) would really
# reboot the device -- this runs unattended on a live, in-use SteamOS
# install during a real update, so that would be even worse than in
# the image-build tool. Drop CAP_SYS_MODULE/CAP_SYS_BOOT/CAP_SYS_RAWIO
# from the bounding set for everything run inside the chroot so those
# syscalls fail safely (EPERM) instead of touching real device state.
CHROOT_DROP_CAPS = 'setpriv --no-new-privs --bounding-set=-sys_module,-sys_boot,-sys_rawio'.split()


def chroot_argv(root, *args):
    """Build a capability-dropped chroot argv for root.

    Args:
        root: Chroot target directory.
        *args: Command and arguments to run inside the chroot.

    Returns:
        The full argv list, including CHROOT_DROP_CAPS.
    """
    return CHROOT_DROP_CAPS + ['chroot', str(root)] + list(args)


def in_chroot(shell_command, **kw):
    """Run shell_command inside the MERGED overlay chroot via /bin/bash -c.

    Args:
        shell_command: The shell command line to run.
        **kw: Extra keyword arguments forwarded to subprocess.run().

    Returns:
        The completed subprocess.CompletedProcess.
    """
    return subprocess.run(chroot_argv(MERGED, '/bin/bash', '-c', shell_command), **kw)


def cleanup():
    """Tear down the build chroot's mounts and scratch files on exit.

    pacman-key --populate spawns gpg-agent/dirmngr chrooted at MERGED
    (or NEWROOT, during the second chroot for grub). They can hold a
    SUBMOUNT like .../dev busy via an open fd without being chrooted
    into that submount specifically -- kill anything actually chrooted
    at the top of each tree up front, since quiet_umount below only
    matches the exact submount path it is unmounting and would
    otherwise miss these every time.
    """
    kill_chrooted(MERGED)
    kill_chrooted(NEWROOT)
    for m in (MERGED / 'dev' / 'pts', MERGED / 'dev', MERGED / 'sys', MERGED / 'proc', MERGED,
              NEWROOT / 'efi', NEWROOT / 'dev' / 'pts', NEWROOT / 'dev', NEWROOT / 'sys',
              NEWROOT / 'proc', NEWROOT, WORK):
        quiet_umount(m)
    run_ok(['sync'])
    for d in (NEWROOT, WORK):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        WORKIMG.unlink()
    except FileNotFoundError:
        pass


atexit.register(cleanup)


def main():
    """Rebuild and install any configured self-healing payload(s) into PARTSET.

    Two independent payloads are supported, each optional and each
    driven by its own config file the LIVE host already has (written by
    NvidiaUsbImageBuilder / AcpiEnabler when that feature was originally
    enabled): the pinned NVIDIA driver (DRIVER_CONF_PATH) and the
    acpi_call DKMS module (ACPI_CALL_CONF_PATH). Either, both, or
    neither may be configured on a given device -- this exits
    immediately (success) if neither is, and only does the work each
    payload still actually needs, independently skipping one if the
    target slot already has it for its kernel.

    The two payloads are fully failure-isolated from each other, but NOT
    symmetrically: acpi_call is a convenience feature, so any failure
    building/installing it just logs a WARNING and is skipped this run --
    it never cancels the update. The NVIDIA driver is not optional in
    that sense: a failure there is fatal and DOES cancel the update (via
    a deferred die() near the end of this function) -- but only after
    acpi_call has already had its own independent attempt, so a driver
    hiccup can never prevent acpi_call from being rebuilt this run, even
    though the update as a whole still gets cancelled if the driver
    couldn't be built.

    Mounts the target partition set's rootfs and EFI partitions, builds
    inside a loopback-backed overlay chroot (working around the
    casefold/overlayfs incompatibility on SteamOS's /home), copies the
    resulting payload(s) into the target rootfs, regenerates its grub
    config if the NVIDIA cmdline needs it, and propagates the
    self-healing machinery itself (this script + both config files +
    the wrapped update binaries) so the next update is covered too.

    Raises:
        SystemExit: Via die(), if the NVIDIA driver was configured but
            could not be built/installed this run.
    """
    try:
        driver_conf = json.loads(Path(DRIVER_CONF_PATH).read_text())
    except (OSError, ValueError):
        driver_conf = {}
    pkg_urls = driver_conf.get('pkg_urls') or []
    driver_version = driver_conf.get('driver_version', '?')
    want_driver = bool(pkg_urls)

    try:
        acpi_conf = json.loads(Path(ACPI_CALL_CONF_PATH).read_text())
    except (OSError, ValueError):
        acpi_conf = {}
    acpi_module = acpi_conf.get('module') or 'acpi_call'
    acpi_repo = acpi_conf.get('repo')
    want_acpi = bool(acpi_repo)

    if not want_driver and not want_acpi:
        log('Neither NVIDIA driver nor acpi_call self-heal is configured on this host -- nothing to do')
        return

    rootdev = Path('/dev/disk/by-partsets/%s/rootfs' % PARTSET)
    efidev = Path('/dev/disk/by-partsets/%s/efi' % PARTSET)
    if not rootdev.is_block_device() or not efidev.is_block_device():
        die("partset '%s' not found (single-slot system?)" % PARTSET)

    try:
        WORKIMG.unlink()
    except FileNotFoundError:
        pass
    run(['truncate', '-s', '8G', str(WORKIMG)])
    run(['mkfs.ext4', '-q', '-F', str(WORKIMG)])
    run(['mount', '-o', 'loop', str(WORKIMG), str(WORK)])
    for d in (UPPER, OVLWORK, MERGED):
        d.mkdir(parents=True, exist_ok=True)

    log('Mounting %s' % rootdev)
    result = subprocess.run(['blkid', '-p', '-s', 'TYPE', '-o', 'value', str(rootdev)],
                             capture_output=True, text=True)
    rootfs_type = result.stdout.strip() or None
    state['rootfs_type'] = rootfs_type
    if rootfs_type == 'btrfs':
        run(['mount', '-o', 'compress-force=zstd:3', str(rootdev), str(NEWROOT)])
    else:
        run(['mount', str(rootdev), str(NEWROOT)])

    if rootfs_type == 'btrfs':
        prop = subprocess.run(['btrfs', 'property', 'get', str(NEWROOT), 'ro'],
                               capture_output=True, text=True).stdout.strip()
        if prop == 'ro=true':
            state['was_ro'] = True
            run(['btrfs', 'property', 'set', str(NEWROOT), 'ro', 'false'])

    def restore_ro():
        """Re-apply the target's original btrfs read-only property, if any."""
        if state['was_ro']:
            run(['btrfs', 'property', 'set', str(NEWROOT), 'ro', 'true'])

    kver = None
    modules_dir = NEWROOT / 'usr' / 'lib' / 'modules'
    if modules_dir.is_dir():
        for d in sorted(modules_dir.iterdir()):
            if d.is_dir() and 'neptune' in d.name.lower():
                kver = d.name
                break
    if not kver:
        die('no neptune kernel in %s rootfs' % PARTSET)
    log('Target kernel: %s' % kver)

    driver_glob = str(NEWROOT / 'usr' / 'lib' / 'modules' / kver / 'updates' / 'dkms' / 'nvidia.ko*')
    need_driver_build = want_driver and not glob.glob(driver_glob)

    acpi_glob = str(NEWROOT / 'usr' / 'lib' / 'modules' / kver / 'updates' / 'dkms' / (acpi_module + '.ko*'))
    need_acpi_build = want_acpi and not glob.glob(acpi_glob)

    if not need_driver_build and not need_acpi_build:
        already = [n for n, w in (('driver', want_driver), (acpi_module, want_acpi)) if w]
        log('%s already up to date for %s -- nothing to do' % (' and '.join(already), kver))
        restore_ro()
        return

    pacdb = NEWROOT / 'usr' / 'lib' / 'holo' / 'pacmandb' / 'local'
    kpkg_dir = None
    if pacdb.is_dir():
        for d in sorted(pacdb.glob('linux-neptune-*-[0-9]*')):
            if not d.is_dir() or re.search(r'-headers-|firmware|rtw', d.name):
                continue
            kpkg_dir = d
            break
    if not kpkg_dir:
        die("kernel package not found in new slot's pacman db")
    kpkg_full = kpkg_dir.name
    kpkg_name = re.sub(r'-[^-]+-[^-]+$', '', kpkg_full)
    kpkg_verrel = kpkg_full[len(kpkg_name) + 1:]

    pacman_conf = (NEWROOT / 'etc' / 'pacman.conf').read_text()
    m = re.search(r'^\[(jupiter-[^\]]+)\]', pacman_conf, re.M)
    jupiter_repo = m.group(1) if m else None
    mirrorlist = (NEWROOT / 'etc' / 'pacman.d' / 'mirrorlist').read_text()
    m = re.search(r'^Server\s*=\s*(\S+)', mirrorlist, re.M)
    mirror = m.group(1) if m else None
    if not jupiter_repo or not mirror:
        die('could not resolve headers mirror/repo')
    hdr_url = mirror.replace('$repo', jupiter_repo).replace('$arch', 'x86_64')
    hdr_url = hdr_url.rstrip('/') + '/%s-headers-%s-x86_64.pkg.tar.zst' % (kpkg_name, kpkg_verrel)
    log('Headers: %s' % os.path.basename(hdr_url))
    if not run_ok(['curl', '-sfIL', hdr_url, '-o', '/dev/null']):
        die("matching headers not in Valve's pool: %s" % hdr_url)

    log('Setting up build overlay chroot')
    run(['mount', '-t', 'overlay', 'overlay', '-o',
         'index=off,lowerdir=%s,upperdir=%s,workdir=%s' % (NEWROOT, UPPER, OVLWORK), str(MERGED)])
    run(['mount', '-t', 'proc', 'proc', str(MERGED / 'proc')])
    run(['mount', '--rbind', '/sys', str(MERGED / 'sys')])
    run(['mount', '--make-rslave', str(MERGED / 'sys')])
    run(['mount', '--rbind', '/dev', str(MERGED / 'dev')])
    run(['mount', '--make-rslave', str(MERGED / 'dev')])
    resolv = MERGED / 'etc' / 'resolv.conf'
    try:
        resolv.unlink()
    except FileNotFoundError:
        pass
    shutil.copy('/etc/resolv.conf', str(resolv))

    if not (MERGED / 'etc' / 'pacman.d' / 'gnupg' / 'private-keys-v1.d').is_dir():
        in_chroot('pacman-key --init && pacman-key --populate', check=True)
    in_chroot("curl -sfL '%s' -o /tmp/headers.pkg.tar.zst" % hdr_url, check=True)
    in_chroot('pacman -Sy', check=True)
    in_chroot("pacman -U --noconfirm --needed --overwrite '*' /tmp/headers.pkg.tar.zst", check=True)
    in_chroot("pacman -S --noconfirm --needed --overwrite '*' dkms", check=True)
    if need_acpi_build:
        # acpi_call is a plain out-of-tree C module built by hand, not
        # via the driver's own pinned pkg_urls (which already carry
        # their own toolchain packages -- see BUILD_ONLY_RE) -- it
        # needs its own compiler, same as AcpiEnabler's live overlay
        # build. Installed before the pkg_urls payload snapshot below
        # so none of its files get mistaken for driver payload.
        #
        # Deliberately NOT check=True: acpi_call is an optional
        # convenience feature, never allowed to take an update down
        # with it. Every other acpi_call step below is already
        # non-fatal (a failure just logs a WARNING and skips it) --
        # this used to be the one exception, and it isn't supposed to
        # be: a failure here used to raise, get caught by the
        # top-level handler, die() the WHOLE script, and cancel the
        # update -- including an otherwise-successful, unrelated
        # NVIDIA driver rebuild -- while the wrapper reported the
        # completely generic "NVIDIA driver rebuild FAILED", even
        # though the driver was never the problem.
        if in_chroot("pacman -S --noconfirm --needed --overwrite '*' base-devel").returncode != 0:
            log('WARNING: base-devel install failed -- skipping acpi_call self-heal this run '
                '(unrelated to the driver, which is unaffected)')
            need_acpi_build = False

    # driver_error stays None on full success. A failure anywhere in this
    # block is deliberately NOT allowed to sys.exit() immediately (unlike
    # the old behavior) -- it's recorded here and the script keeps going
    # so the acpi_call block below still gets its own independent attempt
    # this run, exactly like an acpi_call failure never blocks the driver.
    # The driver is still treated as a true fatal condition overall: see
    # the deferred die(driver_error) near the end of main(), after both
    # payloads have had their chance.
    driver_error = None
    if need_driver_build:
        try:
            log('Building driver in overlay chroot (this takes 10-20 minutes)')
            before_pkgs = set(in_chroot('pacman -Qq', check=True, capture_output=True, text=True).stdout.split())

            log('Installing pinned driver %s' % driver_version)
            in_chroot('mkdir -p /tmp/nvpkgs', check=True)
            for url in pkg_urls:
                if in_chroot("curl -sfL '%s' -o /tmp/nvpkgs/$(basename '%s')" % (url, url)).returncode != 0:
                    raise RuntimeError('download failed: %s' % url)

            if in_chroot("pacman -U --noconfirm --needed --overwrite '*' /tmp/nvpkgs/*.pkg.tar.zst").returncode != 0:
                # unattended context: a keyring mismatch (frozen image keyring vs
                # current Arch packager keys) must not brick updates -- packages
                # came over HTTPS from Arch infrastructure, so retry unsigned
                # rather than fail the update
                log('WARNING: pacman -U failed (keyring?) -- retrying with signature checks off')
                pacman_conf_text = (MERGED / 'etc' / 'pacman.conf').read_text()
                nosig = re.sub(r'^SigLevel.*', 'SigLevel = Never', pacman_conf_text, flags=re.M)
                (MERGED / 'tmp' / 'pacman-nosig.conf').write_text(nosig)
                if in_chroot("pacman --config /tmp/pacman-nosig.conf -U --noconfirm --needed "
                             "--overwrite '*' /tmp/nvpkgs/*.pkg.tar.zst").returncode != 0:
                    raise RuntimeError('driver package install failed')

            driver_ko_glob = str(MERGED / 'usr' / 'lib' / 'modules' / kver / 'updates' / 'dkms' / 'nvidia.ko*')
            if not glob.glob(driver_ko_glob):
                in_chroot('dkms autoinstall -k %s' % kver, check=True)
            if not glob.glob(driver_ko_glob):
                raise RuntimeError('driver failed to build for %s' % kver)
            after_pkgs = set(in_chroot('pacman -Qq', check=True, capture_output=True, text=True).stdout.split())

            new_pkgs = sorted(p for p in (after_pkgs - before_pkgs) if not BUILD_ONLY_RE.match(p))
            if not new_pkgs:
                raise RuntimeError('payload list empty')
            log('Payload: %s' % ' '.join(new_pkgs))

            files_rel = []
            for pkg in new_pkgs:
                listing = in_chroot('pacman -Qlq %s' % pkg, check=True, capture_output=True, text=True).stdout
                files_rel.extend(line.lstrip('/') for line in listing.splitlines() if line)

            log('Copying driver into %s rootfs' % PARTSET)
            files_list_path = WORK / 'files.rel'
            files_list_path.write_text('\n'.join(files_rel) + '\n')
            run(['rsync', '-a', '--files-from=%s' % files_list_path, str(MERGED) + '/', str(NEWROOT) + '/'])
            updates_src = UPPER / 'usr' / 'lib' / 'modules' / kver / 'updates'
            if updates_src.is_dir():
                run(['rsync', '-a', str(updates_src), str(NEWROOT / 'usr' / 'lib' / 'modules' / kver) + '/'])
            for pkg in new_pkgs:
                local_db = UPPER / 'usr' / 'lib' / 'holo' / 'pacmandb' / 'local'
                for entry in sorted(local_db.glob('%s-[0-9]*' % pkg)):
                    if entry.is_dir():
                        run(['rsync', '-a', str(entry),
                             str(NEWROOT / 'usr' / 'lib' / 'holo' / 'pacmandb' / 'local') + '/'])
                        break

            (NEWROOT / 'etc' / 'modprobe.d' / '99-nvidia-patch.conf').write_text(
                '# Added by steamos-utils repatch\n'
                'blacklist nouveau\n'
                'options nouveau modeset=0\n'
                'options nvidia-drm modeset=1 fbdev=1\n'
                'options nvidia NVreg_PreserveVideoMemoryAllocations=1\n'
            )
            run_ok(chroot_argv(NEWROOT, 'systemctl', 'enable',
                               'nvidia-suspend', 'nvidia-resume', 'nvidia-hibernate'))
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see comment above
            driver_error = str(exc)
            log('WARNING: driver build failed this run -- %s -- acpi_call (if configured) still '
                'gets its own independent attempt below; this update will still be cancelled '
                'once that finishes, since an NVIDIA driver failure is fatal' % driver_error)

    if need_acpi_build:
        log('Resolving latest %s release' % acpi_module)
        acpi_version, acpi_tag = resolve_acpi_call_version(acpi_repo)
        if not acpi_version:
            log('WARNING: could not resolve a release tag for %s -- skipping it this run' % acpi_repo)
        else:
            log('Building %s %s against %s' % (acpi_module, acpi_version, kver))
            src_rel = 'usr/src/%s-%s' % (acpi_module, acpi_version)
            chroot_src_dir = MERGED / src_rel
            run_ok(['rm', '-rf', str(chroot_src_dir)])
            clone_ok = (in_chroot("git clone --depth 1 --branch %s '%s' '/%s'"
                                  % (acpi_tag, acpi_repo, src_rel)).returncode == 0
                        and chroot_src_dir.is_dir())
            if not clone_ok:
                log('WARNING: clone of %s failed -- skipping %s this run' % (acpi_repo, acpi_module))
            else:
                (chroot_src_dir / 'dkms.conf').write_text(
                    'PACKAGE_NAME="%s"\n'
                    'PACKAGE_VERSION="%s"\n'
                    'BUILT_MODULE_NAME[0]="%s"\n'
                    'DEST_MODULE_LOCATION[0]="/kernel/drivers/platform/x86"\n'
                    'AUTOINSTALL="yes"\n'
                    % (acpi_module, acpi_version, acpi_module)
                )
                in_chroot('dkms add -m %s -v %s -k %s' % (acpi_module, acpi_version, kver))
                in_chroot('dkms build -m %s -v %s -k %s' % (acpi_module, acpi_version, kver))
                status = in_chroot('dkms status -m %s -v %s -k %s' % (acpi_module, acpi_version, kver),
                                    capture_output=True, text=True)
                if 'built' not in (status.stdout or ''):
                    log('WARNING: %s did not build for %s -- see log above, skipping it this run'
                        % (acpi_module, kver))
                else:
                    # dkms install runs INSIDE the build chroot, not
                    # against NEWROOT directly -- NEWROOT is a fresh OS
                    # slot that may not have dkms installed at all (the
                    # same problem AcpiEnabler._ensure_host_dkms() works
                    # around on the live system). Writes land in UPPER
                    # via the overlay's copy-up, then get rsynced into
                    # NEWROOT directly, same as the driver's own module
                    # files above.
                    in_chroot('dkms install -m %s -v %s -k %s' % (acpi_module, acpi_version, kver))
                    acpi_ko_glob = str(MERGED / 'usr' / 'lib' / 'modules' / kver / 'updates' / 'dkms'
                                       / (acpi_module + '.ko*'))
                    if not glob.glob(acpi_ko_glob):
                        log('WARNING: %s did not install for %s -- see log above, skipping it this run'
                            % (acpi_module, kver))
                    else:
                        # Copy-back errors below are also non-fatal, same
                        # reasoning as the base-devel install above: a
                        # copy hiccup for this optional module must not
                        # cancel an otherwise-successful driver update.
                        log('Installing %s %s into %s rootfs' % (acpi_module, acpi_version, PARTSET))
                        copy_ok = True
                        updates_src = UPPER / 'usr' / 'lib' / 'modules' / kver / 'updates'
                        if updates_src.is_dir():
                            copy_ok &= run_ok(['rsync', '-a', str(updates_src),
                                               str(NEWROOT / 'usr' / 'lib' / 'modules' / kver) + '/'])
                        src_upper = UPPER / src_rel
                        if src_upper.is_dir():
                            copy_ok &= run_ok(['mkdir', '-p', str((NEWROOT / src_rel).parent)])
                            copy_ok &= run_ok(['rsync', '-a', str(src_upper) + '/',
                                               str(NEWROOT / src_rel) + '/'])
                        dkms_rel = 'var/lib/dkms/%s/%s' % (acpi_module, acpi_version)
                        dkms_upper = UPPER / dkms_rel
                        if dkms_upper.is_dir():
                            copy_ok &= run_ok(['mkdir', '-p', str((NEWROOT / dkms_rel).parent)])
                            copy_ok &= run_ok(['rsync', '-a', str(dkms_upper) + '/',
                                               str(NEWROOT / dkms_rel) + '/'])
                        if not copy_ok:
                            log('WARNING: %s copy-back into %s had errors -- see above'
                                % (acpi_module, PARTSET))
                        try:
                            modconf = NEWROOT / 'etc' / 'modules-load.d' / (acpi_module + '.conf')
                            modconf.parent.mkdir(parents=True, exist_ok=True)
                            modconf.write_text(acpi_module + '\n')
                        except OSError as exc:
                            log('WARNING: could not write modules-load.d entry for %s: %s'
                                % (acpi_module, exc))
                        log('%s ready for %s' % (acpi_module, kver))

    run(chroot_argv(NEWROOT, 'depmod', kver))
    run(chroot_argv(NEWROOT, 'ldconfig'))

    if need_driver_build and driver_error is None:
        default_grub = NEWROOT / 'etc' / 'default' / 'grub'
        grub_text = default_grub.read_text()
        if 'rd.driver.blacklist=nouveau' not in grub_text:
            grub_text = re.sub(r'^(GRUB_CMDLINE_LINUX_DEFAULT=")', r'\1' + CMDLINE_ADD + ' ',
                                grub_text, flags=re.M)
            default_grub.write_text(grub_text)

    # propagate the self-healing machinery (this script + whichever of
    # driver.json/acpi_call.json exist) so the NEXT update is covered
    # too -- copies EVERYTHING under steamos-utils/ wholesale, so this
    # needed no changes to add acpi_call.json support above
    dest_lib = NEWROOT / 'usr' / 'lib' / 'steamos-utils'
    dest_lib.mkdir(parents=True, exist_ok=True)
    for item in Path('/usr/lib/steamos-utils').iterdir():
        dest_item = dest_lib / item.name
        if item.is_dir():
            shutil.copytree(str(item), str(dest_item), dirs_exist_ok=True)
        else:
            shutil.copy2(str(item), str(dest_item))
    for name in ('steamos-update', 'steamos-update-os', 'steamos-atomupd-client'):
        new_bin = NEWROOT / 'usr' / 'bin' / name
        new_orig = NEWROOT / 'usr' / 'bin' / (name + '.orig')
        src_bin = Path('/usr/bin') / name
        if not new_bin.exists() or not src_bin.exists():
            continue
        if not new_orig.exists():
            shutil.move(str(new_bin), str(new_orig))
            shutil.copy2(str(src_bin), str(new_bin))
            new_bin.chmod(0o755)
    oobe_service = (NEWROOT / 'usr' / 'lib' / 'systemd' / 'system'
                    / 'steamos-finish-oobe-migration.service')
    if oobe_service.exists():
        link = NEWROOT / 'etc' / 'systemd' / 'system' / 'steamos-finish-oobe-migration.service'
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to('/dev/null')
    sudoers_src = Path('/etc/sudoers.d/zz-deck-nopasswd')
    if sudoers_src.exists():
        sudoers_dst = NEWROOT / 'etc' / 'sudoers.d' / 'zz-deck-nopasswd'
        shutil.copy2(str(sudoers_src), str(sudoers_dst))
        sudoers_dst.chmod(0o440)

    if need_driver_build and driver_error is None:
        # regenerate the new slot's grub.cfg with the nvidia cmdline --
        # only the driver needs a cmdline change; acpi_call just needs
        # modprobe at boot (modules-load.d, written above), no grub
        # involvement at all
        try:
            log('Regenerating grub config for %s' % PARTSET)
            (NEWROOT / 'efi').mkdir(parents=True, exist_ok=True)
            run(['mount', str(efidev), str(NEWROOT / 'efi')])
            run(['mount', '-t', 'proc', 'proc', str(NEWROOT / 'proc')])
            run(['mount', '--rbind', '/sys', str(NEWROOT / 'sys')])
            run(['mount', '--make-rslave', str(NEWROOT / 'sys')])
            run(['mount', '--rbind', '/dev', str(NEWROOT / 'dev')])
            run(['mount', '--make-rslave', str(NEWROOT / 'dev')])
            run(chroot_argv(NEWROOT, 'update-grub'))
            grub_cfg = NEWROOT / 'efi' / 'EFI' / 'steamos' / 'grub.cfg'
            if 'rd.driver.blacklist=nouveau' not in grub_cfg.read_text():
                raise RuntimeError('regenerated grub.cfg is missing the nvidia cmdline')
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see driver_error comment above
            driver_error = str(exc)
            log('WARNING: grub regeneration failed this run -- %s' % driver_error)

    log('Syncing')
    if rootfs_type == 'btrfs':
        run_ok(['btrfs', 'filesystem', 'sync', str(NEWROOT)])
    run(['sync', '-f', str(NEWROOT)])
    restore_ro()

    if driver_error is not None:
        # Deferred from the driver-build/grub-regen sections above so that
        # acpi_call (if configured) always gets its own independent
        # attempt in the same run, regardless of the driver's outcome --
        # the driver itself is still a true fatal condition, so the
        # update is cancelled here, just later than before.
        die('driver build failed: %s' % driver_error)

    log('OK -- %s is ready (%s)' % (PARTSET, kver))


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- last-resort unattended guard
        die(str(exc))
