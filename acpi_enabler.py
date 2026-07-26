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

import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dkms_supported_versions import get_kernel_headers_filename
from dkms_supported_versions import get_kernel_modules_filename
from dkms_supported_versions import get_os_version
from package_downloader import PackageDownloader
from shell_utils import run_command


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
    repatch_script.py already uses on-device for the NVIDIA driver
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
    replaces the old file_downloader.check_mirror_and_download_package(),
    which hit a single hardcoded Valve mirror URL with no atomic write
    and no local caching.

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
    # repatch_script.py use for every chroot invocation: chroot() only
    # changes the filesystem root, not the kernel/module/device
    # namespace, so a process inside the build chroot that calls
    # modprobe/insmod/rmmod or reboot(2) is still talking to this
    # machine's one real running kernel. Nothing the build legitimately
    # needs (compiling, dkms build/add) requires these.
    CHROOT_DROP_CAPS = 'setpriv --no-new-privs --bounding-set=-sys_module,-sys_boot,-sys_rawio'.split()

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
        self.workdir = Path(workdir) if workdir else Path('acpi-enabler-work')

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

    # -------------------------------------------------------------- steps
    def prep_steamos(self):
        self._log('Disabling SteamOS read-only filesystem')
        run_command(['sudo', 'steamos-readonly', 'disable'], self.verbose)

    def finalize_steamos(self):
        self._log('Re-enabling SteamOS read-only filesystem')
        run_command(['sudo', 'steamos-readonly', 'enable'], self.verbose)

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
        run_command(['sudo', 'pacman-key', '--init'], self.verbose)
        run_command(['sudo', 'pacman-key', '--populate'], self.verbose)

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
        run_command(
            [
                'sudo', 'pacman', '-U', '--dbpath', str(self.DB_PATH),
                # --needed: skip a package outright if the exact same
                # version is already installed, instead of forcing a
                # reinstall ("up to date -- reinstalling"). Matches the
                # same flag NvidiaUsbImageBuilder/repatch_script.py
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
            self.verbose,
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
        result = run_command(
            ['dkms', 'status', '-m', self.ACPI_CALL_MODULE, '-k', kernel_release],
            self.verbose,
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
        result = run_command(
            ['git', 'ls-remote', '--tags', '--refs', self.ACPI_CALL_REPO],
            self.verbose,
        )
        if result is None or not result.stdout:
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
    # This whole section mirrors nvidia_usb_image_builder/repatch_script.py's
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
        return subprocess.run(['mountpoint', '-q', str(path)]).returncode == 0

    def _kill_chrooted(self, target):
        """Kill only processes actually chrooted into target.

        Matches via /proc/<pid>/root, never via fuser -km -- see
        repatch_script.py's kill_chrooted() for why: target's dev/sys
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
        if subprocess.run(['sudo', 'umount', '-R', str(path)]).returncode == 0:
            return
        subprocess.run(['sudo', 'umount', '-Rl', str(path)])

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
        run_command(['sudo', 'truncate', '-s', self.BUILD_IMG_SIZE, str(self.BUILD_IMG)], self.verbose)
        run_command(['sudo', 'mkfs.ext4', '-q', '-F', str(self.BUILD_IMG)], self.verbose)
        run_command(['sudo', 'mount', '-o', 'loop', str(self.BUILD_IMG), str(work)], self.verbose)
        for directory in (upper, ovlwork, merged):
            directory.mkdir(parents=True, exist_ok=True)

        run_command(
            ['sudo', 'mount', '-t', 'overlay', 'overlay', '-o',
             'index=off,lowerdir=/,upperdir=%s,workdir=%s' % (upper, ovlwork),
             str(merged)],
            self.verbose,
        )
        run_command(['sudo', 'mount', '-t', 'proc', 'proc', str(merged / 'proc')], self.verbose)
        run_command(['sudo', 'mount', '--rbind', '/sys', str(merged / 'sys')], self.verbose)
        run_command(['sudo', 'mount', '--make-rslave', str(merged / 'sys')], self.verbose)
        run_command(['sudo', 'mount', '--rbind', '/dev', str(merged / 'dev')], self.verbose)
        run_command(['sudo', 'mount', '--make-rslave', str(merged / 'dev')], self.verbose)

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
        run_command(['sudo', 'rm', '-f', str(resolv)], self.verbose)
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
        for mount in (merged / 'dev' / 'pts', merged / 'dev', merged / 'sys',
                      merged / 'proc', merged, work):
            self._quiet_umount(mount)
        subprocess.run(['sudo', 'sync'])
        try:
            work.rmdir()
        except OSError:
            pass
        self.BUILD_IMG.unlink(missing_ok=True)
        self._build_work = None
        self._build_merged = None

    def _in_build_chroot(self, shell_command):
        """Run shell_command inside the build overlay chroot.

        Args:
            shell_command: The shell command line to run.

        Returns:
            The completed subprocess.CompletedProcess on success, or
            None if it failed (see shell_utils.run_command()).
        """
        return run_command(
            self.CHROOT_DROP_CAPS
            + ['chroot', str(self._build_merged), '/bin/bash', '-c', shell_command],
            self.verbose,
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
        kernel_release = platform.release()
        if self._acpi_call_installed(kernel_release):
            self._log('acpi_call already installed for %s' % kernel_release)
            run_command(['sudo', 'modprobe', self.ACPI_CALL_MODULE], self.verbose)
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
            run_command(['sudo', 'rm', '-rf', str(host_src_dir)], self.verbose)
        if host_dkms_dir.exists():
            self._warn('Removing stale leftover %s from an earlier attempt' % host_dkms_dir)
            run_command(['sudo', 'rm', '-rf', str(host_dkms_dir)], self.verbose)

        self._log('Setting up disposable build overlay on %s' % self.BUILD_IMG)
        self._setup_build_overlay()
        try:
            chroot_src_dir = self._build_merged / module_rel_path

            self._log('Fetching acpi_call %s' % tag)
            run_command(
                ['sudo', 'git', 'clone', '--depth', '1', '--branch', tag,
                 self.ACPI_CALL_REPO, str(chroot_src_dir)],
                self.verbose,
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

            built_check = self._in_build_chroot(
                'dkms status -m %s -v %s -k %s' % (self.ACPI_CALL_MODULE, version, kernel_release)
            )
            if built_check is None or 'built' not in (built_check.stdout or ''):
                self._die(
                    'acpi_call did not build successfully inside the overlay -- see the '
                    'dkms build log above (still inside the throwaway overlay, so this '
                    "doesn't cost any real root space either way)"
                )
                return

            # Copy just the two small, tiny artifacts dkms actually
            # needs -- NOT the toolchain that built them -- from the
            # overlay's upper layer back onto the real, live root.
            self._log('Copying the built module back onto the real root')
            host_src_dir.parent.mkdir(parents=True, exist_ok=True)
            host_dkms_dir.parent.mkdir(parents=True, exist_ok=True)
            run_command(['sudo', 'rm', '-rf', str(host_src_dir)], self.verbose)
            run_command(['sudo', 'rm', '-rf', str(host_dkms_dir)], self.verbose)
            run_command(
                ['sudo', 'cp', '-a', str(chroot_src_dir), str(host_src_dir)], self.verbose,
            )
            chroot_dkms_dir = self._build_merged / dkms_rel_path
            if chroot_dkms_dir.is_dir():
                run_command(
                    ['sudo', 'cp', '-a', str(chroot_dkms_dir), str(host_dkms_dir)],
                    self.verbose,
                )
        finally:
            self._log('Tearing down the build overlay (freeing the toolchain space back)')
            self._teardown_build_overlay()

        # From here on, everything runs against the real root. dkms
        # finds the copied-back /var/lib/dkms/acpi_call/<version> state
        # already marked "built" for this exact kernel, so `dkms
        # install` only copies the module into place + runs depmod --
        # it never needs a compiler here.
        self._log('Installing acpi_call %s' % version)
        run_command(
            ['sudo', 'dkms', 'install', '-m', self.ACPI_CALL_MODULE,
             '-v', version, '-k', kernel_release],
            self.verbose,
        )

        if not self._acpi_call_installed(kernel_release):
            self._die('dkms install did not report acpi_call as installed for %s'
                       % kernel_release)
            return

        self._log('Loading acpi_call module')
        run_command(['sudo', 'modprobe', self.ACPI_CALL_MODULE], self.verbose)

        # dkms keeps the built module around across kernel updates, but
        # something still has to load it at boot -- modules-load.d is
        # the standard systemd mechanism for that.
        modules_load_conf = Path('/etc/modules-load.d/acpi_call.conf')
        modules_load_conf.write_text(self.ACPI_CALL_MODULE + '\n')

    def enable(self):
        """Run the full enable flow.

        Disables read-only, downloads + installs the kernel
        modules/headers packages matching the running kernel, builds
        and registers the acpi_call DKMS module against them, then
        restores read-only and cleans up the downloaded packages.

        Raises:
            RuntimeError: If any step fails (e.g. required packages
                cannot be resolved, downloaded, or installed).
        """
        self._log('Enabling ACPI calls')
        self.prep_steamos()
        modules_path, headers_path = self.download_kernel_packages()
        self.install_kernel_packages(modules_path, headers_path)
        self.install_acpi_call_module()
        self.finalize_steamos()
        self.cleanup(modules_path, headers_path)
