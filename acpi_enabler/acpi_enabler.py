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

    file: acpi_enabler.py
'''

"""Enables Linux DKMS ACPI calls on the running SteamOS system."""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from common.lib.dkms_supported_versions import get_kernel_headers_filename
from common.lib.dkms_supported_versions import get_kernel_modules_filename
from common.lib.dkms_supported_versions import get_os_version
from common.lib.package_downloader import PackageDownloader


class AcpiEnabler:
    """Enables Linux Dynamic Kernel Module Support ACPI calls on SteamOS.

    Downloads the linux-neptune kernel modules + headers packages
    matching the currently running kernel and installs them via pacman
    (bracketed by disabling/re-enabling steamos-readonly), then builds
    and registers the actual acpi_call DKMS module — the out-of-tree
    module (https://github.com/nix-community/acpi_call) that exposes
    /proc/acpi/call for raw ACPI method calls from userspace, which is
    the thing this class is actually meant to enable. It isn't in
    Valve's pacman repos, so it's built from source, then registered
    with DKMS and loaded (see install_acpi_call_module()).

    Building acpi_call needs a full C toolchain (gcc, binutils, ...),
    which is a couple hundred MB installed -- too much for SteamOS's
    root partition, which typically only has a small margin of free
    space. Installing it there directly can fail outright with "not
    enough free disk space", and even when it fits, it's wasted
    permanent space for something only needed once, briefly, at build
    time. So the whole toolchain + build instead lives inside a
    disposable overlay backed by a loopback image on /home (which has
    real free space -- game storage), the exact same technique
    common/selfheal/repatch_script.py already uses on-device for the NVIDIA driver
    build, just overlaid on the live root here instead of a mounted
    target partition. Only the resulting tiny build artifacts (the
    compiled module + its dkms bookkeeping) get copied back onto the
    real root; the toolchain itself is discarded with the overlay,
    so "/" never grows by more than a few hundred KB.

    Restructured into a self-contained class (matching NvidiaUsbImageBuilder's
    style — constructor holds verbose, small colored logging helpers, one
    method per step, an enable() entry point) rather than a set of loose
    module-level functions each taking their own dry_run argument.

    Package downloads go through the shared PackageDownloader (see that
    module) — the exact same atomic-download-with-caching mechanism
    NvidiaUsbImageBuilder uses to fetch NVIDIA driver packages. Unlike
    those, though, these are Valve's own SteamOS kernel builds, not
    general Arch packages, so the download URL isn't resolved via
    archlinux.org/pin_pkg() (those packages aren't published there at
    all) — it's resolved from the running system's OWN
    /etc/pacman.conf + /etc/pacman.d/mirrorlist, the same jupiter-repo +
    mirror-template approach NvidiaUsbImageBuilder.resolve_headers_url()
    uses against a mounted image's pacman.conf/mirrorlist, just read
    directly off the live filesystem here instead of a chroot. This
    replaces the old (now-removed) file_downloader.py's
    check_mirror_and_download_package(), which hit a single hardcoded
    Valve mirror URL with no atomic write and no local caching.

    Attributes:
        verbose: Whether underlying shell commands print their output.
        workdir: Scratch directory used for downloaded packages.
        os_version: Parsed running-kernel version info, set by
            resolve_kernel_packages().
        kernel_modules_filename: Resolved kernel modules package
            filename, set by resolve_kernel_packages().
        kernel_headers_filename: Resolved kernel headers package
            filename, set by resolve_kernel_packages().
    """

    PACMAN_CONF = Path('/etc/pacman.conf')
    MIRRORLIST = Path('/etc/pacman.d/mirrorlist')

    # Upstream source for the acpi_call kernel module. Not packaged by
    # Valve or Arch, so it's built from source rather than resolved via
    # pin_pkg()/pacman like everything else this project installs.
    ACPI_CALL_REPO = 'https://github.com/nix-community/acpi_call.git'
    ACPI_CALL_MODULE = 'acpi_call'

    # SteamOS's live package database is NOT at pacman's default
    # /var/lib/pacman -- it lives here instead (same path
    # nvidia_usb_image_builder.py and the recovery/ scripts already use
    # for mounted-image installs). Installing against the default path
    # hits an uninitialized keyring that was never meant to be used,
    # which surfaces as a confusing "keyring is not writable" / "required
    # key missing from keyring" pacman error rather than a clear one.
    DB_PATH = Path('/usr/lib/holo/pacmandb')

    # Directory pacman-key actually creates once a keyring has been
    # initialized. Used to skip re-running pacman-key --init/--populate
    # (which is slow) once it's already been done.
    KEYRING_DIR = Path('/etc/pacman.d/gnupg/private-keys-v1.d')

    # Loopback image backing the disposable build overlay -- lives on
    # /home (real free space), never on the size-constrained root. 3G
    # comfortably fits a full C toolchain plus build artifacts; it's
    # freed again in full once the overlay is torn down.
    BUILD_IMG = Path('/home/.acpi-call-build.img')
    BUILD_IMG_SIZE = '3G'

    # Same capability-bounding-set drop NvidiaUsbImageBuilder and
    # common/selfheal/repatch_script.py use for every chroot invocation: chroot() only
    # changes the filesystem root, not the kernel/module/device
    # namespace, so a process inside the build chroot that calls
    # modprobe/insmod/rmmod or reboot(2) is still talking to this
    # machine's one real running kernel. Nothing the build legitimately
    # needs (compiling, dkms build/add) requires these.
    CHROOT_DROP_CAPS = 'setpriv --no-new-privs --bounding-set=-sys_module,-sys_boot,-sys_rawio'.split()

    # Shared self-heal directory + repatch script -- the SAME ones
    # NvidiaUsbImageBuilder._configure_selfheal_updates() uses (see that
    # method, and common/selfheal/repatch_script.py's own module
    # docstring). Reusing them rather than standing up a parallel
    # acpi-only mechanism means one shared repatch.py already knows how
    # to rebuild BOTH the NVIDIA driver and acpi_call, and a device that
    # ends up with both features enabled only gets ONE set of wrapped
    # update binaries, not two competing ones.
    SELFHEAL_LIB_DIR = Path('/usr/lib/steamos-utils')
    ACPI_CALL_CONF_PATH = SELFHEAL_LIB_DIR / 'acpi_call.json'
    # .parent.parent: this file lives at acpi_enabler/acpi_enabler.py,
    # so climbing one 'parent' only reaches acpi_enabler/ -- the second
    # 'parent' reaches the project root, where common/ is a sibling.
    _SELFHEAL_DIR = Path(__file__).resolve().parent.parent / 'common' / 'selfheal'
    REPATCH_SCRIPT = _SELFHEAL_DIR / 'repatch_script.py'
    UPDATE_WRAPPER_SCRIPT = _SELFHEAL_DIR / 'update_wrapper.py'

    _C_RESET = '\033[0m'
    _C_LOG = '\033[1;35m'     # magenta — [acpi-enabler] detail lines
    _C_WARN = '\033[1;33m'    # yellow  — [warn]
    _C_FAIL = '\033[1;31m'    # red     — [fail]

    def __init__(self, workdir=None, verbose=False):
        """Initialize the enabler.

        Args:
            workdir: Scratch directory for downloaded packages. Defaults
                to ".acpi-enabler-work" in the current directory.
            verbose: If True, print each underlying shell command as it
                runs.
        """
        self.verbose = verbose
        self.workdir = Path(workdir) if workdir else Path('.acpi-enabler-work')

        self.os_version = None
        self.kernel_modules_filename = None
        self.kernel_headers_filename = None

        # Set while the disposable build overlay (see
        # _setup_build_overlay()/_teardown_build_overlay()) is mounted.
        self._build_work = None
        self._build_merged = None

        self._downloader = PackageDownloader(
            self.workdir / 'pkgs', verbose=self.verbose,
            log=self._log, warn=self._warn,
        )

    # ------------------------------------------------------------ logging
    def _colorize(self, color, text):
        if not sys.stdout.isatty():
            return text
        return '%s%s%s' % (color, text, self._C_RESET)

    def _log(self, message):
        print('%s %s' % (self._colorize(self._C_LOG, '[acpi-enabler]'), message))

    def _warn(self, message):
        print('%s %s' % (self._colorize(self._C_WARN, '[warn]'), message), file=sys.stderr)

    def _die(self, message):
        raise RuntimeError('%s %s' % (self._colorize(self._C_FAIL, '[fail]'), message))

    # -------------------------------------------------------- running commands
    # Same _run()/_run_quiet() split NvidiaUsbImageBuilder uses (and for the
    # same reason shell_utils.run_command() was retired project-wide):
    # run_command() unconditionally printed a "Shell command failed" message
    # on ANY non-zero exit, with no way to say "this one's allowed to fail,
    # don't warn" -- exactly wrong for the several routine, expected-to-
    # sometimes-fail checks in this class (mountpoint -q, an unmount that
    # was never mounted, a dkms status query for a kernel that doesn't have
    # it built yet). It also returned None on any failure instead of the
    # real CompletedProcess, which several callers here need in order to
    # inspect .stdout/.returncode even when check=False.
    def _run(self, command, check=True):
        """Run command, warning (but not raising) on an unexpected failure.

        Args:
            command: Argv list to execute.
            check: If True (default), a non-zero exit or missing
                executable is treated as unexpected and a warning is
                printed. If False, failures are silent (see
                _run_quiet()).

        Returns:
            The completed subprocess.CompletedProcess, or None if the
            command's executable could not be found.
        """
        display = ' '.join(str(part) for part in command)
        if self.verbose:
            print('  $ %s' % display)
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError as exc:
            if check:
                self._warn('command not found: %s (%s)' % (display, exc))
            return None

        if result.returncode != 0:
            if check:
                self._warn('command failed (exit %d): %s' % (result.returncode, display))
                stderr = (result.stderr or '').strip()
                for line in stderr.splitlines():
                    print('      %s' % line, file=sys.stderr)
        elif self.verbose:
            stdout = (result.stdout or '').strip()
            for line in stdout.splitlines():
                print('      %s' % line)
        return result

    def _run_quiet(self, command):
        """Run a command where a non-zero exit is a routine, expected outcome.

        Examples: dkms status for a kernel that hasn't built it yet, an
        unmount of something never mounted -- never warns on failure.

        Args:
            command: Argv list to execute.

        Returns:
            The completed subprocess.CompletedProcess, or None if the
            command's executable could not be found.
        """
        return self._run(command, check=False)

    # -------------------------------------------------------------- steps
    def prep_steamos(self):
        self._log('Disabling SteamOS read-only filesystem')
        self._run(['sudo', 'steamos-readonly', 'disable'])

    def finalize_steamos(self):
        self._log('Re-enabling SteamOS read-only filesystem')
        self._run(['sudo', 'steamos-readonly', 'enable'])

    def _resolve_kernel_package_url(self, filename):
        """Resolve the download URL for a kernel modules/headers package.

        Builds the download URL for filename (a linux-neptune-* kernel
        modules/headers package) from the running system's OWN pacman
        configuration. Mirrors NvidiaUsbImageBuilder.resolve_headers_url(),
        which does the same jupiter-repo + $repo/$arch mirror-template
        substitution against a mounted image's pacman.conf/mirrorlist —
        here it's read directly off the live filesystem instead.

        Args:
            filename: The linux-neptune-* package filename to resolve a
                URL for.

        Returns:
            The resolved download URL, or None if it could not be
            resolved (in which case _die() has already raised).
        """
        if not self.PACMAN_CONF.exists() or not self.MIRRORLIST.exists():
            self._die('%s or %s not found — is this actually SteamOS?'
                       % (self.PACMAN_CONF, self.MIRRORLIST))
            return None

        conf_text = self.PACMAN_CONF.read_text(errors='ignore')
        match = re.search(r'^\[(jupiter-[^\]]+)\]', conf_text, re.M)
        jupiter_repo = match.group(1) if match else None
        if not jupiter_repo:
            self._die('No jupiter repo in %s' % self.PACMAN_CONF)
            return None

        mirrorlist_text = self.MIRRORLIST.read_text(errors='ignore')
        match = re.search(r'^Server\s*=\s*(\S+)', mirrorlist_text, re.M)
        mirror = match.group(1) if match else None
        if not mirror:
            self._die('No mirror server found in %s' % self.MIRRORLIST)
            return None

        base = mirror.replace('$repo', jupiter_repo).replace('$arch', 'x86_64')
        return '%s/%s' % (base.rstrip('/'), filename)

    def resolve_kernel_packages(self):
        """Work out the running kernel's exact package filenames and URLs.

        Returns:
            A (modules_url, headers_url) tuple.
        """
        self.os_version = get_os_version()
        self.kernel_modules_filename = get_kernel_modules_filename(self.os_version)
        self.kernel_headers_filename = get_kernel_headers_filename(self.os_version)
        self._log('Running kernel: %s (%s)'
                   % (self.os_version['kernel_long_version'], self.os_version['kernel_type']))

        modules_url = self._resolve_kernel_package_url(self.kernel_modules_filename)
        headers_url = self._resolve_kernel_package_url(self.kernel_headers_filename)

        if modules_url and not self._downloader.url_exists(modules_url):
            self._die('Kernel modules package not found: %s' % modules_url)
        if headers_url and not self._downloader.url_exists(headers_url):
            self._die('Kernel headers package not found: %s' % headers_url)

        return modules_url, headers_url

    def download_kernel_packages(self):
        modules_url, headers_url = self.resolve_kernel_packages()
        try:
            modules_path = self._downloader.download(modules_url, self.kernel_modules_filename) \
                if modules_url else None
            headers_path = self._downloader.download(headers_url, self.kernel_headers_filename) \
                if headers_url else None
        except RuntimeError as exc:
            self._die(str(exc))
            return None, None
        return modules_path, headers_path

    def _ensure_pacman_keyring(self):
        """Initialize + populate the pacman keyring if it isn't already.

        SteamOS doesn't normally expect anyone to run `pacman -U`
        directly against the live system, so on many devices
        /etc/pacman.d/gnupg has never been initialized. Installing
        against an uninitialized keyring fails with a confusing
        "keyring is not writable" / "required key missing from keyring"
        pacman error rather than a clear one, so check for it up front
        and fix it before that happens. Mirrors the identical safeguard
        NvidiaUsbImageBuilder.build_driver() already applies inside its
        build chroot, just against the live filesystem instead.
        """
        if self.KEYRING_DIR.is_dir():
            return
        self._log('Initialising pacman keyring')
        self._run(['sudo', 'pacman-key', '--init'])
        self._run(['sudo', 'pacman-key', '--populate'])

    def install_kernel_packages(self, modules_path, headers_path):
        """Install the downloaded kernel modules/headers packages via pacman.

        Args:
            modules_path: Local path to the downloaded kernel modules
                package, or None.
            headers_path: Local path to the downloaded kernel headers
                package, or None.

        Raises:
            RuntimeError: If neither package was successfully downloaded.
        """
        self._log('Installing kernel modules + headers packages')
        packages = [str(p) for p in (modules_path, headers_path) if p]
        if not packages:
            self._die('Nothing to install — package resolution/download failed earlier')
            return
        self._ensure_pacman_keyring()
        self._run(
            [
                'sudo', 'pacman', '-U', '--dbpath', str(self.DB_PATH),
                # --needed: skip a package outright if the exact same
                # version is already installed, instead of forcing a
                # reinstall ("up to date -- reinstalling"). Matches the
                # same flag NvidiaUsbImageBuilder/common/selfheal/repatch_script.py
                # already use for every pacman -U/-S call in the build
                # chroot.
                '--needed',
                # --overwrite '*': the modules/headers packages' files
                # (and pahole, pulled in as a dkms-toolchain dependency)
                # can legitimately already exist on disk untracked by
                # holo's package database -- e.g. installed once before
                # this --dbpath fix landed, or bundled directly into the
                # base image outside holo's tracking. Without this,
                # pacman refuses the transaction with "conflicting
                # files"/"exists in filesystem" rather than just
                # reclaiming ownership of files that are already correct.
                '--overwrite', '*',
            ]
            + packages + ['--noconfirm'],
        )

    def cleanup(self, modules_path, headers_path):
        self._log('Cleaning up downloaded packages')
        for path in (modules_path, headers_path):
            if path:
                Path(path).unlink(missing_ok=True)

    # -------------------------------------------------------- acpi_call
    def _acpi_call_installed(self, kernel_release):
        """Check whether acpi_call is already dkms-installed for a kernel.

        Args:
            kernel_release: The kernel release string to check against
                (e.g. platform.release()).

        Returns:
            True if `dkms status` reports acpi_call as installed for
            kernel_release.
        """
        # _run_quiet: "not installed for this kernel yet" is the routine,
        # expected outcome the very first time this runs, not a failure.
        result = self._run_quiet(
            ['dkms', 'status', '-m', self.ACPI_CALL_MODULE, '-k', kernel_release],
        )
        return bool(result and 'installed' in (result.stdout or ''))

    def _resolve_acpi_call_version(self):
        """Resolve the latest release tag of acpi_call from its git repo.

        Returns:
            A (version, tag) tuple, e.g. ('1.2.2', 'v1.2.2').

        Raises:
            RuntimeError: If the repo's tags can't be listed, or none of
                them look like a release version.
        """
        result = self._run(['git', 'ls-remote', '--tags', '--refs', self.ACPI_CALL_REPO])
        if result is None or result.returncode != 0 or not result.stdout:
            self._die('Could not list release tags for %s' % self.ACPI_CALL_REPO)
            return None

        best = None
        for line in result.stdout.splitlines():
            # Only match proper "vX.Y.Z" release tags. The repo also
            # carries legacy tags named after a supported KERNEL version
            # rather than a module release (e.g. "3.17", "kernel_3_17"),
            # which sort numerically ABOVE real releases like v1.2.2 as
            # a bare (3, 17) > (1, 2, 2) tuple, but aren't valid
            # `git clone --branch` refs under a "v"-prefixed name --
            # requiring the literal "v" excludes those entirely.
            match = re.search(r'refs/tags/(v[0-9]+(?:\.[0-9]+)*)$', line)
            if not match:
                continue
            tag = match.group(1)
            version = tag[1:]
            version_tuple = tuple(int(part) for part in version.split('.'))
            if best is None or version_tuple > best[0]:
                best = (version_tuple, version, tag)
        if best is None:
            self._die('No release tags found in %s' % self.ACPI_CALL_REPO)
            return None

        return best[1], best[2]

    # ------------------------------------------ disposable build overlay
    # This whole section mirrors common/selfheal/repatch_script.py's
    # proven overlay-chroot build machinery (same mount sequence, same
    # /proc/<pid>/root-based process killing instead of `fuser -km`, same
    # capability-dropped chroot) -- the only real difference is the
    # overlay's lowerdir is the LIVE running "/" here instead of a
    # separately-mounted target partition, since there's no other rootfs
    # to build against on a live single-boot device.
    def _is_mountpoint(self, path):
        """Check whether path is currently a mountpoint.

        Args:
            path: Path to check.

        Returns:
            True if path is a mountpoint, False otherwise.
        """
        result = self._run_quiet(['mountpoint', '-q', str(path)])
        return result is not None and result.returncode == 0

    def _kill_chrooted(self, target):
        """Kill only processes actually chrooted into target.

        Matches via /proc/<pid>/root, never via fuser -km -- see
        common/selfheal/repatch_script.py's kill_chrooted() for why: target's dev/sys
        are --rbind mounts of this machine's REAL /dev and /sys (same
        inodes, not copies), so `fuser -km` against a path containing
        them can kill any process on the system with so much as
        /dev/null open.

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

    def _quiet_umount(self, path):
        """Unmount path if mounted, killing chrooted holders first.

        Args:
            path: Path to unmount.
        """
        if not self._is_mountpoint(path):
            return
        self._kill_chrooted(path)
        time.sleep(0.2)
        result = self._run_quiet(['sudo', 'umount', '-R', str(path)])
        if result is not None and result.returncode == 0:
            return
        self._run_quiet(['sudo', 'umount', '-Rl', str(path)])

    def _setup_build_overlay(self):
        """Mount a disposable build overlay backed by a loop image on /home.

        lowerdir is the live "/" itself (read-only reference; the real
        root stays mounted and running, untouched), upperdir/workdir
        live inside a throwaway ext4 loopback image on /home so nothing
        written during the build (a whole C toolchain, in particular)
        ever consumes root's own free space. Sets self._build_work and
        self._build_merged.

        Raises:
            RuntimeError: If the overlay chroot doesn't come up with a
                working /bin/bash (mount failure).
        """
        work = self.workdir / 'buildwork'
        work.mkdir(parents=True, exist_ok=True)
        upper = work / 'upper'
        ovlwork = work / 'ovlwork'
        merged = work / 'merged'

        self.BUILD_IMG.unlink(missing_ok=True)
        self._run(['sudo', 'truncate', '-s', self.BUILD_IMG_SIZE, str(self.BUILD_IMG)])
        self._run(['sudo', 'mkfs.ext4', '-q', '-F', str(self.BUILD_IMG)])
        self._run(['sudo', 'mount', '-o', 'loop', str(self.BUILD_IMG), str(work)])
        for directory in (upper, ovlwork, merged):
            directory.mkdir(parents=True, exist_ok=True)

        self._run(
            ['sudo', 'mount', '-t', 'overlay', 'overlay', '-o',
             'index=off,lowerdir=/,upperdir=%s,workdir=%s' % (upper, ovlwork),
             str(merged)],
        )
        self._run(['sudo', 'mount', '-t', 'proc', 'proc', str(merged / 'proc')])
        self._run(['sudo', 'mount', '--rbind', '/sys', str(merged / 'sys')])
        self._run(['sudo', 'mount', '--make-rslave', str(merged / 'sys')])
        self._run(['sudo', 'mount', '--rbind', '/dev', str(merged / 'dev')])
        self._run(['sudo', 'mount', '--make-rslave', str(merged / 'dev')])

        # dkms's bookkeeping tree at /var/lib/dkms has to be the REAL,
        # persistent one on the host, not whatever lowerdir=/ resolves
        # to at that path. Confirmed on real hardware: once dkms was
        # already installed on the host (see _ensure_host_dkms()),
        # `dkms add`/`dkms build` run INSIDE this overlay started
        # failing immediately with "No write access to DKMS tree at
        # /var/lib/dkms" -- almost certainly the same class of problem
        # the explicit /proc, /sys, /dev rbind mounts above already
        # exist to work around: /var lives on its own mount on
        # SteamOS, and overlayfs's lowerdir=/ does not descend into a
        # separate mount nested inside it -- it only sees whatever (if
        # anything) sits at that path on the underlying root
        # filesystem itself, which is not the writable, live /var.
        # Bind mounting the real /var/lib/dkms straight through
        # sidesteps that ambiguity entirely, and as a bonus means the
        # build's dkms bookkeeping lands directly on the real host --
        # no separate copy-back step needed for it afterwards (see
        # install_acpi_call_module()).
        host_dkms_root = Path('/var/lib/dkms')
        host_dkms_root.mkdir(parents=True, exist_ok=True)
        chroot_dkms_root = merged / 'var' / 'lib' / 'dkms'
        self._run(['sudo', 'mkdir', '-p', str(chroot_dkms_root)])
        self._run(['sudo', 'mount', '--bind', str(host_dkms_root), str(chroot_dkms_root)])

        # /etc/resolv.conf on a systemd-based system is almost always a
        # symlink into /run/systemd/resolve/... , and /run is a tmpfs --
        # not part of the underlying root filesystem's on-disk content --
        # so lowerdir=/ can't see through to its live contents here,
        # breaking DNS resolution (pacman) inside the chroot. Same fix
        # NvidiaUsbImageBuilder.setup_overlay_chroot() already applies
        # for its own overlay chroot: drop the (dangling, from here)
        # symlink and replace it with a real copy of the resolved
        # content instead.
        resolv = merged / 'etc' / 'resolv.conf'
        self._run(['sudo', 'rm', '-f', str(resolv)])
        try:
            shutil.copy('/etc/resolv.conf', resolv)
        except OSError:
            pass

        if not (merged / 'bin' / 'bash').exists():
            self._die(
                '%s has no /bin/bash -- the build overlay did not mount '
                'correctly (see the mount error above)' % merged
            )
            return

        self._build_work = work
        self._build_merged = merged

    def _teardown_build_overlay(self):
        """Unmount the build overlay and delete its loop image.

        Frees all the space the disposable toolchain used, in full.
        Safe to call even if _setup_build_overlay() was never called or
        already torn down.
        """
        merged = self._build_merged
        work = self._build_work
        if merged is None or work is None:
            return

        # pacman-key --populate (if it ever runs here) spawns
        # gpg-agent/dirmngr chrooted at merged, which can hold a
        # submount busy via an open fd without being chrooted into that
        # submount specifically -- kill anything chrooted at the top of
        # the tree up front, since _quiet_umount() below only matches
        # the exact submount path it's unmounting.
        self._kill_chrooted(merged)
        for mount in (merged / 'var' / 'lib' / 'dkms', merged / 'dev' / 'pts', merged / 'dev',
                      merged / 'sys', merged / 'proc', merged, work):
            self._quiet_umount(mount)
        self._run(['sudo', 'sync'])
        try:
            work.rmdir()
        except OSError:
            pass
        self.BUILD_IMG.unlink(missing_ok=True)
        self._build_work = None
        self._build_merged = None

    def _in_build_chroot(self, shell_command, check=True):
        """Run shell_command inside the build overlay chroot.

        Args:
            shell_command: The shell command line to run.
            check: Forwarded to _run() -- False for status checks where a
                non-zero exit is a routine, expected outcome (see
                install_acpi_call_module()'s dkms status check below).

        Returns:
            The completed subprocess.CompletedProcess, or None if the
            command could not be executed at all.
        """
        return self._run(
            self.CHROOT_DROP_CAPS
            + ['chroot', str(self._build_merged), '/bin/bash', '-c', shell_command],
            check=check,
        )

    def _ensure_host_dkms(self):
        """Install dkms onto the real host root if it isn't already there.

        `dkms` gets installed inside the DISPOSABLE build overlay too
        (alongside base-devel, see install_acpi_call_module()), but
        that copy is discarded along with the rest of the throwaway
        toolchain the moment the overlay is torn down. Every dkms
        invocation this class makes OUTSIDE the overlay -- the
        idempotency check in _acpi_call_installed(), and the final
        `dkms install` at the end of install_acpi_call_module() -- runs
        against the real host, so `dkms` has to actually be present
        there too, or those calls fail with a plain "dkms: command not
        found" (seen on devices where dkms had never been installed on
        the live root before). Unlike gcc/base-devel, dkms itself is a
        small script/service package, not a couple-hundred-MB
        toolchain, so it's fine to install it for real and keep it
        rather than routing it through the disposable overlay too.

        Raises:
            RuntimeError: If dkms still isn't on PATH after attempting
                to install it.
        """
        if shutil.which('dkms') is not None:
            return
        self._log('Installing dkms onto the real root (kept permanently -- it is small)')
        self._ensure_pacman_keyring()
        self._run(
            ['sudo', 'pacman', '-S', 'dkms', '--dbpath', str(self.DB_PATH),
             '--needed', '--overwrite', '*', '--noconfirm'],
        )
        if shutil.which('dkms') is None:
            self._die(
                '"dkms" still not found on PATH after installing the dkms package -- '
                'is this actually SteamOS/Arch-based?'
            )

    # -------------------------------------------------------- acpi_call
    def install_acpi_call_module(self):
        """Build and register the acpi_call DKMS module for this kernel.

        Idempotent: if `dkms status` already reports acpi_call as
        installed for the running kernel, this only (re)loads it and
        returns.

        Otherwise, the ENTIRE build -- cloning the source, installing a
        full C toolchain + dkms via pacman, and running `dkms add`/
        `dkms build` -- happens inside a disposable overlay (see
        _setup_build_overlay()) so none of that permanently touches
        root's own free space. Once the build succeeds inside the
        overlay, only the two small directories dkms actually needs
        (the source+built-module tree under /usr/src, and dkms's own
        bookkeeping under /var/lib/dkms) are copied from the overlay
        back onto the real root -- not the toolchain that built them.
        The overlay is then torn down completely (freeing all the
        toolchain's space back), and `dkms install` runs for real
        against the copied-back, already-built state, so it never
        needs a compiler on the real root at all.

        Raises:
            RuntimeError: If git isn't available, the module's release
                tags can't be resolved, the build fails inside the
                overlay, or the final dkms install doesn't report
                acpi_call as installed.
        """
        self._ensure_host_dkms()

        kernel_release = platform.release()
        if self._acpi_call_installed(kernel_release):
            self._log('acpi_call already installed for %s' % kernel_release)
            self._run(['sudo', 'modprobe', self.ACPI_CALL_MODULE])
            return

        if shutil.which('git') is None:
            self._die("git is required to build acpi_call and wasn't found on PATH")
            return

        version, tag = self._resolve_acpi_call_version()
        module_rel_path = Path('usr') / 'src' / ('%s-%s' % (self.ACPI_CALL_MODULE, version))
        dkms_rel_path = Path('var') / 'lib' / 'dkms' / self.ACPI_CALL_MODULE / version
        host_src_dir = Path('/') / module_rel_path
        host_dkms_dir = Path('/') / dkms_rel_path

        # We already confirmed above that dkms does NOT consider this
        # version installed, so anything already sitting at these real
        # host paths can only be incomplete debris from an earlier
        # failed attempt (e.g. a partial clone before a build error).
        # Clear it before starting: since the overlay's lowerdir is the
        # live "/" itself, a stale non-empty leftover here would
        # otherwise still be visible inside the overlay and make the
        # clone below fail with "already exists and is not an empty
        # directory".
        if host_src_dir.exists():
            self._warn('Removing stale leftover %s from an earlier attempt' % host_src_dir)
            self._run(['sudo', 'rm', '-rf', str(host_src_dir)])
        if host_dkms_dir.exists():
            self._warn('Removing stale leftover %s from an earlier attempt' % host_dkms_dir)
            self._run(['sudo', 'rm', '-rf', str(host_dkms_dir)])

        self._log('Setting up disposable build overlay on %s' % self.BUILD_IMG)
        self._setup_build_overlay()
        try:
            chroot_src_dir = self._build_merged / module_rel_path

            self._log('Fetching acpi_call %s' % tag)
            self._run(
                ['sudo', 'git', 'clone', '--depth', '1', '--branch', tag,
                 self.ACPI_CALL_REPO, str(chroot_src_dir)],
            )
            if not chroot_src_dir.is_dir():
                self._die('Clone of %s failed' % self.ACPI_CALL_REPO)
                return

            # Written ourselves (overriding anything the repo ships) so
            # the version registered with dkms always matches the
            # directory name -- dkms requires PACKAGE_VERSION here to
            # equal the "-<version>" suffix on /usr/src/<module>-<version>.
            dkms_conf = (
                'PACKAGE_NAME="%s"\n'
                'PACKAGE_VERSION="%s"\n'
                'BUILT_MODULE_NAME[0]="%s"\n'
                'DEST_MODULE_LOCATION[0]="/kernel/drivers/platform/x86"\n'
                'AUTOINSTALL="yes"\n'
                % (self.ACPI_CALL_MODULE, version, self.ACPI_CALL_MODULE)
            )
            (chroot_src_dir / 'dkms.conf').write_text(dkms_conf)

            # Even though the real host's pacman keyring is already
            # initialized (visible here via lowerdir=/), GPG's keyring
            # state doesn't reliably carry over through the overlay +
            # chroot boundary in practice (stale agent sockets, etc.) --
            # it surfaces as the same "keyring is not writable"/"Public
            # keyring not found" error _ensure_pacman_keyring() already
            # works around for the live system. Since this environment
            # is disposable anyway, just always initialise a fresh one
            # here rather than trying to detect whether the inherited
            # one actually works.
            self._log('Initialising pacman keyring inside the build overlay')
            self._in_build_chroot('pacman-key --init && pacman-key --populate')

            self._log('Installing a throwaway build toolchain (discarded after the build)')
            self._in_build_chroot(
                'pacman -Sy --dbpath %s && '
                'pacman -S base-devel dkms --dbpath %s --needed --overwrite "*" --noconfirm'
                % (self.DB_PATH, self.DB_PATH)
            )

            self._log('Building acpi_call %s against %s' % (version, kernel_release))
            self._in_build_chroot(
                'dkms add -m %s -v %s && dkms build -m %s -v %s -k %s'
                % (self.ACPI_CALL_MODULE, version, self.ACPI_CALL_MODULE, version, kernel_release)
            )

            # check=False: the failure path right below already handles
            # and reports this explicitly, so this shouldn't also print
            # its own separate "command failed" warning on top of that.
            built_check = self._in_build_chroot(
                'dkms status -m %s -v %s -k %s' % (self.ACPI_CALL_MODULE, version, kernel_release),
                check=False,
            )
            if built_check is None or 'built' not in (built_check.stdout or ''):
                self._die(
                    'acpi_call did not build successfully inside the overlay -- see the '
                    'dkms build log above (still inside the throwaway overlay, so this '
                    "doesn't cost any real root space either way)"
                )
                return

            # Only /usr/src needs copying back -- the tiny source +
            # built-module tree, NOT the toolchain that built it, from
            # the overlay's upper layer onto the real, live root.
            # /var/lib/dkms needs no copy-back at all: it was bind
            # mounted straight through to the real host for the whole
            # build (see _setup_build_overlay()), so dkms's bookkeeping
            # was already being written directly onto the real tree the
            # entire time -- host_dkms_dir already holds the just-built
            # state as-is.
            self._log('Copying the built module source back onto the real root')
            host_src_dir.parent.mkdir(parents=True, exist_ok=True)
            self._run(['sudo', 'rm', '-rf', str(host_src_dir)])
            self._run(['sudo', 'cp', '-a', str(chroot_src_dir), str(host_src_dir)])
        finally:
            self._log('Tearing down the build overlay (freeing the toolchain space back)')
            self._teardown_build_overlay()

        # From here on, everything runs against the real root. dkms
        # finds the /var/lib/dkms/acpi_call/<version> state -- already
        # on the real root the whole time, via the bind mount set up in
        # _setup_build_overlay() -- marked "built" for this exact
        # kernel, so `dkms install` only copies the module into place
        # + runs depmod -- it never needs a compiler here.
        self._log('Installing acpi_call %s' % version)
        self._run(
            ['sudo', 'dkms', 'install', '-m', self.ACPI_CALL_MODULE,
             '-v', version, '-k', kernel_release],
        )

        if not self._acpi_call_installed(kernel_release):
            self._die('dkms install did not report acpi_call as installed for %s'
                       % kernel_release)
            return

        self._log('Loading acpi_call module')
        self._run(['sudo', 'modprobe', self.ACPI_CALL_MODULE])

        # dkms keeps the built module around across kernel updates, but
        # something still has to load it at boot -- modules-load.d is
        # the standard systemd mechanism for that.
        modules_load_conf = Path('/etc/modules-load.d/acpi_call.conf')
        modules_load_conf.write_text(self.ACPI_CALL_MODULE + '\n')

    # ------------------------------------------------------- self-heal
    def _configure_selfheal_updates(self):
        """Make acpi_call survive future OS updates (self-healing).

        A SteamOS update doesn't patch the running root -- it stages a
        whole new OS image into the OTHER partition set and reboots
        into it, so acpi_call (installed here only onto the CURRENTLY
        running slot) would simply be gone after the next update,
        exactly like the NVIDIA driver would be without
        NvidiaUsbImageBuilder's selfheal mode. This sets up the exact
        same fix, applied live instead of at image-build time: writes
        acpi_call.json (the module name + upstream repo repatch.py
        needs to rebuild it), makes sure the shared repatch.py is
        installed, and wraps whichever real update entry points exist
        on this device (steamos-update always; steamos-update-os and
        steamos-atomupd-client if present -- matches
        NvidiaUsbImageBuilder._configure_selfheal_updates()'s reasoning
        for wrapping all three: an update triggered through any one of
        them must not skip the self-heal machinery).

        If NvidiaUsbImageBuilder's own selfheal machinery is already
        installed on this device (from a prior NVIDIA-patched image),
        this only adds acpi_call.json alongside the existing
        driver.json -- the wrapper and repatch.py are shared, so
        nothing about the NVIDIA side is touched or duplicated.

        Raises:
            RuntimeError: If repatch_script.py or update_wrapper.py is
                missing from this project's own install (should ship
                under common/).
        """
        self._log('Installing self-healing update machinery for acpi_call')
        self.SELFHEAL_LIB_DIR.mkdir(parents=True, exist_ok=True)

        self.ACPI_CALL_CONF_PATH.write_text(json.dumps({
            'comment': 'Written by AcpiEnabler at enable time. repatch.py rebuilds '
                       'acpi_call from this info after each OS update.',
            'module': self.ACPI_CALL_MODULE,
            'repo': self.ACPI_CALL_REPO,
        }, indent=2) + '\n')

        if not self.REPATCH_SCRIPT.exists():
            self._die('%s is missing -- reinstall/redownload SteamOS-Utils '
                       '(repatch_script.py should ship under common/)'
                       % self.REPATCH_SCRIPT)
            return

        if not self.UPDATE_WRAPPER_SCRIPT.exists():
            self._die('%s is missing -- reinstall/redownload SteamOS-Utils '
                       '(update_wrapper.py should ship under common/)'
                       % self.UPDATE_WRAPPER_SCRIPT)
            return

        # Always refresh: this project ships exactly one repatch.py, so
        # copying it is idempotent regardless of whether NvidiaUsbImageBuilder
        # already installed one first -- either way the device ends up
        # with the current, up-to-date script that knows how to handle
        # BOTH payloads.
        repatch_path = self.SELFHEAL_LIB_DIR / 'repatch.py'
        shutil.copy(self.REPATCH_SCRIPT, repatch_path)
        repatch_path.chmod(0o755)

        for name in ('steamos-update', 'steamos-update-os', 'steamos-atomupd-client'):
            binpath = Path('/usr/bin') / name
            if name != 'steamos-update' and not binpath.exists():
                continue
            origpath = Path('/usr/bin') / (name + '.orig')
            if not origpath.exists() and binpath.exists():
                # First device to wrap this entry point -- preserve the
                # real binary. If it's already wrapped (orig already
                # exists, e.g. from a prior NvidiaUsbImageBuilder
                # selfheal setup), leave the preserved original alone
                # and just refresh the wrapper content below.
                shutil.move(str(binpath), str(origpath))
            elif not origpath.exists():
                continue
            shutil.copy(self.UPDATE_WRAPPER_SCRIPT, binpath)
            binpath.chmod(0o755)

    def enable(self):
        """Run the full enable flow.

        Disables read-only, downloads + installs the kernel
        modules/headers packages matching the running kernel, builds
        and registers the acpi_call DKMS module against them, installs
        the self-healing update machinery so it survives future OS
        updates, then restores read-only and cleans up the downloaded
        packages.

        Raises:
            RuntimeError: If any step fails (e.g. required packages
                cannot be resolved, downloaded, or installed).
        """
        self._log('Enabling ACPI calls')
        self.prep_steamos()
        modules_path, headers_path = self.download_kernel_packages()
        self.install_kernel_packages(modules_path, headers_path)
        self.install_acpi_call_module()
        self._configure_selfheal_updates()
        self.finalize_steamos()
        self.cleanup(modules_path, headers_path)

    # ------------------------------------------------------------ disable
    def _unload_acpi_call_module(self):
        """Unload acpi_call from the running kernel and stop loading it at boot.

        Best-effort: acpi_call may not currently be loaded (e.g. after a
        reboot before disable() was run), which is not an error here.
        """
        self._log('Unloading acpi_call module')
        self._run_quiet(['sudo', 'modprobe', '-r', self.ACPI_CALL_MODULE])

        modules_load_conf = Path('/etc/modules-load.d/acpi_call.conf')
        if modules_load_conf.exists():
            self._run_quiet(['sudo', 'rm', '-f', str(modules_load_conf)])

    def _remove_acpi_call_dkms(self):
        """Unregister acpi_call from dkms for every version it's built as.

        Parses `dkms status -m acpi_call` rather than assuming the
        latest upstream release tag (_resolve_acpi_call_version()) is
        what's actually installed -- a device can be running an older
        built version than whatever's currently tagged upstream.
        Best-effort/non-fatal per version, same reasoning as everywhere
        else acpi_call is treated as an optional convenience feature
        that must never block the rest of an operation.
        """
        result = self._run_quiet(['dkms', 'status', '-m', self.ACPI_CALL_MODULE])
        if result is None or not result.stdout:
            return
        versions = sorted(set(re.findall(r'^%s/([^,]+),' % re.escape(self.ACPI_CALL_MODULE),
                                          result.stdout, re.M)))
        for version in versions:
            self._log('Removing acpi_call %s from dkms' % version)
            self._run_quiet(
                ['sudo', 'dkms', 'remove', '-m', self.ACPI_CALL_MODULE, '-v', version, '--all'],
            )

    def _teardown_selfheal_updates(self):
        """Undo _configure_selfheal_updates(), scoped to acpi_call only.

        Always removes ACPI_CALL_CONF_PATH. The shared machinery
        (repatch.py, the wrapped update binaries) is only torn down if
        NVIDIA driver self-heal isn't ALSO configured on this device
        (i.e. DRIVER_CONF_PATH doesn't exist) -- see
        common/selfheal/repatch_script.py's DRIVER_CONF_PATH -- since
        NvidiaUsbImageBuilder's own self-heal depends on that exact same
        machinery and must keep working if this device has both features
        enabled. When acpi_call was the only payload configured, this
        restores the real steamos-update/steamos-update-os/
        steamos-atomupd-client binaries from their preserved <name>.orig
        backups and removes repatch.py, leaving the device exactly as it
        was before self-heal was ever installed.
        """
        try:
            self.ACPI_CALL_CONF_PATH.unlink()
        except FileNotFoundError:
            pass

        driver_conf_path = self.SELFHEAL_LIB_DIR / 'driver.json'
        if driver_conf_path.exists():
            self._log('NVIDIA driver self-heal is still configured on this device -- '
                       'leaving the shared update wrapper and repatch.py in place')
            return

        self._log('acpi_call was the only self-heal payload configured -- '
                   'removing the update wrapper entirely')
        for name in ('steamos-update', 'steamos-update-os', 'steamos-atomupd-client'):
            binpath = Path('/usr/bin') / name
            origpath = Path('/usr/bin') / (name + '.orig')
            if not origpath.exists():
                continue
            shutil.move(str(origpath), str(binpath))
            binpath.chmod(0o755)

        repatch_path = self.SELFHEAL_LIB_DIR / 'repatch.py'
        try:
            repatch_path.unlink()
        except FileNotFoundError:
            pass

        try:
            self.SELFHEAL_LIB_DIR.rmdir()
        except OSError:
            # Not empty (e.g. driver.json raced back in, or some other
            # file lives there) or doesn't exist -- either way, nothing
            # more to do here.
            pass

    def disable(self):
        """Undo enable(): remove acpi_call and, if it's the last payload, self-heal.

        Idempotent and safe to run even if acpi_call was never enabled
        (each step no-ops or logs a best-effort warning rather than
        raising) or if NVIDIA driver self-heal is also configured on
        this device, in which case the shared update wrapper and
        repatch.py are deliberately left alone.
        """
        if not self.ACPI_CALL_CONF_PATH.exists():
            self._log('acpi_call self-heal is not configured on this device -- nothing to disable')
            return

        self._log('Disabling ACPI calls')
        self.prep_steamos()
        self._unload_acpi_call_module()
        self._remove_acpi_call_dkms()
        self._teardown_selfheal_updates()
        self.finalize_steamos()
