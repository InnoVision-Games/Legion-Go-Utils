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

    file: nvidia_usb_image_builder.py

    Python port of https://github.com/28allday/steamos-nvidia-installer
    (steamos-nvidia-installer.sh). Turns a CLEAN SteamOS OOBE repair image
    into a one-click, self-healing USB installer with the NVIDIA-open
    (RTX 20xx+) driver baked in.

    Everything that has a real Python equivalent (Arch package resolution
    and JSON parsing, glibc-version comparison, text patching of grub.cfg /
    repair_device.sh, writing the repatch/installer scripts) is done in
    pure Python. Operations that are inherently OS/root-level (loop
    devices, btrfs, overlayfs mounts, chroot, pacman, rsync, dkms) are
    still shelled out via subprocess, through this class's own quiet
    _run()/_run_quiet() wrappers (see the note near their definitions for
    why those don't reuse shell_utils.run_command).
'''

"""Builds a self-healing NVIDIA-patched SteamOS USB installer image."""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

# PackageDownloader is a top-level sibling of steam_os_utils.py (shared
# with AcpiEnabler — see that module for why), not part of this package,
# so this is an absolute import rather than a relative one.
from package_downloader import PackageDownloader

from .update_wrapper_script_builder import UpdateWrapperScriptBuilder

# Note: this module intentionally does NOT use shell_utils.run_command for
# its shell-outs. That helper unconditionally prints a command's stdout and
# stderr — fine for the simple, rare commands the other modules in this
# project run, but this builder shells out constantly (mount, blkid,
# pacman, rsync, ...) and many of those calls are routine status checks
# where a non-zero exit is normal. Printing every one of them produced a
# wall of blank lines and scary-looking "failures" for things that weren't
# actually wrong. See _run()/_run_quiet() below instead.

socket.setdefaulttimeout(10)


class NvidiaUsbImageBuilder:
    """Builds a SteamOS-NVIDIA one-click USB installer image.

    Builds from a clean SteamOS OOBE repair image.

    Example:
        builder = NvidiaUsbImageBuilder(
            image_path='steamdeck-oobe-repair-3.8.img',
            driver_spec='latest',
            update_mode='selfheal',
            add_installer=True,
            trim_cuda=False,
            skip_sigcheck=False,
            workdir=None,
        )
        builder.build()

    Attributes:
        image_path: Path to the input clean SteamOS OOBE repair image.
        driver_spec: 'latest' or an Arch version prefix to pin the
            NVIDIA driver to.
        update_mode: One of 'selfheal', 'hold', or 'stock'.
        add_installer: Whether to add the one-click USB installer.
        trim_cuda: Whether to drop CUDA/OpenCL/NVVM/OptiX libraries.
        skip_sigcheck: Whether to disable pacman signature checks in the
            build chroot.
        verbose: Whether to print each underlying shell command as it
            runs.
        workdir: Build working directory.
        output_path: Explicit output image path, if given.
        out_image: Resolved output image path, set by
            resolve_image_path().
    """

    ARCHIVE_URL = 'https://archive.archlinux.org/packages'

    # Support packages that Valve's frozen mirror predates and can only
    # come from Arch (egl-wayland2 only became a dependency at branch 590).
    ARCH_ONLY_DEPS = {'egl-wayland2'}

    # Packages pulled in only to build the driver, never shipped in the image.
    BUILD_ONLY_RE = re.compile(
        r'^(dkms|nvidia-open-dkms|patch|gcc|gcc-libs|make|binutils|libisl|'
        r'libmpc|mpfr|pahole|python-setuptools|linux-neptune.*-headers|.*-headers)$'
    )

    REQUIRED_TOOLS = [
        'losetup', 'blkid', 'btrfs', 'rsync', 'curl', 'depmod', 'sed',
        'awk', 'tar', 'zstd', 'pacman', 'readelf', 'chroot', 'mount',
        'umount', 'udevadm', 'findmnt', 'mountpoint', 'mkfs.ext4', 'truncate',
        'blockdev', 'setpriv',
    ]

    # chroot() only changes the filesystem root — it does NOT give the
    # build chroot its own kernel/module/device namespace. A process inside
    # $MERGED that calls modprobe/insmod/rmmod is still talking to THIS
    # machine's real running kernel (there's only one), and reboot(2) would
    # really reboot the host. dkms/pacman post-install hooks for
    # nvidia-open-dkms are exactly the kind of thing that can do this
    # unprompted (a driver package sanity-loading the module it just
    # built, or a hook reloading something). Confirmed the hard way: this
    # was crashing the desktop session (kwin EGL_BAD_PARAMETER / pipewire
    # "invalid memory type" — a real GPU/DRM context loss on the BUILD
    # HOST, not the target image) on a run where the build tool itself was
    # running on a live SteamOS/KDE desktop. Every command run inside the
    # build chroot now drops CAP_SYS_MODULE (no module load/unload),
    # CAP_SYS_BOOT (no reboot/kexec), and CAP_SYS_RAWIO (no raw device
    # I/O) from its capability bounding set via setpriv, so those syscalls
    # fail safely (EPERM) instead of touching real host state. This does
    # not block anything the driver build/install legitimately needs.
    CHROOT_DROP_CAPS = 'setpriv --no-new-privs --bounding-set=-sys_module,-sys_boot,-sys_rawio'.split()

    CMDLINE_ADD = (
        'rd.driver.blacklist=nouveau modprobe.blacklist=nouveau '
        'nvidia-drm.modeset=1 nvidia-drm.fbdev=1'
    )

    # Unlike the update wrapper (update_wrapper_script_builder.py), neither
    # install_to_hd.sh nor repatch.py takes any build-time value from this
    # class that actually varies — repatch.py's CMDLINE_ADD/BUILD_ONLY_RE/
    # DRIVER_CONF_PATH are always the same fixed values this class itself
    # uses, so there was nothing being templated. Both ship as real
    # standalone scripts alongside this file instead of generated Python
    # strings; install_one_click_installer() / _configure_selfheal_updates()
    # just copy them into the image verbatim.
    INSTALL_TO_HD_SCRIPT = Path(__file__).resolve().parent / 'install_to_hd.sh'
    REPATCH_SCRIPT = Path(__file__).resolve().parent / 'repatch_script.py'

    def __init__(self, image_path=None, driver_spec='latest', update_mode='selfheal',
                 add_installer=True, trim_cuda=False, skip_sigcheck=False,
                 workdir=None, verbose=False, output_path=None):
        """Initialize the builder.

        Args:
            image_path: Path to the clean SteamOS OOBE repair .img to
                patch. If None, auto-detected next to this module.
            driver_spec: 'latest' (default) or an Arch version prefix,
                e.g. 580 / 580.105.08.
            update_mode: One of 'selfheal' (default), 'hold', or
                'stock' — how OS updates interact with the driver.
            add_installer: Whether to add the one-click USB installer.
            trim_cuda: Whether to drop CUDA/OpenCL/NVVM/OptiX libraries
                to shrink the image.
            skip_sigcheck: Whether to disable pacman signature checks in
                the build chroot.
            workdir: Build working directory. Defaults to alongside the
                output image.
            verbose: If True, print each underlying shell command as it
                runs.
            output_path: Path for the built installer image. Defaults to
                the input image's filename with "-nvidia" appended.
        """
        self.image_path = image_path
        self.driver_spec = driver_spec
        self.update_mode = update_mode          # selfheal | hold | stock
        self.add_installer = add_installer
        self.trim_cuda = trim_cuda
        self.skip_sigcheck = skip_sigcheck
        self.verbose = verbose

        self.workdir = Path(workdir) if workdir else None
        # If not given, resolve_image_path() defaults this to the input
        # image's own name with "-nvidia" appended, e.g.
        # steamdeck-oobe-repair-3.8.img -> steamdeck-oobe-repair-3.8-nvidia.img
        self.output_path = Path(output_path) if output_path else None
        self.out_image = None
        # Set once self.workdir is known — see prepare_workdir().
        self._downloader = None

        # Populated during build()
        self.mnt = None
        self.efimnt = None
        self.homemnt = None
        self.upper = None
        self.ovlwork = None
        self.merged = None
        self.loopdev = None
        self.root_part = None
        self.efi_part = None
        self.home_part = None
        self.root_fs_type = None
        self.buildfs_img = None
        self.buildfs_mnt = None
        self._had_unclean_unmount = False
        self.kernel_version = None
        self.headers_url = None
        self.driver_version = None
        self.nv_pkgver = None
        self.pkg_urls = []
        self.pkg_files = []
        self.new_packages = []

        # Source generator for the on-device update wrapper, kept as its
        # own class (see update_wrapper_script_builder.py) rather than a
        # method here, since it genuinely varies per binary_name. repatch.py
        # has no such per-call variation (see REPATCH_SCRIPT above), so it
        # ships as a real file instead of needing a generator class here.
        self._update_wrapper_builder = UpdateWrapperScriptBuilder()

    # ------------------------------------------------------------ logging
    # Kept close to the coloring the original bash script used
    # (log()/warn()/die() with \e[1;35m / \e[1;33m / \e[1;31m), but only
    # emitted when stdout is a real terminal so redirected/log-file output
    # doesn't fill up with raw escape codes.
    _C_RESET = '\033[0m'
    _C_BANNER = '\033[1;36m'   # bold cyan  — phase banners
    _C_LOG = '\033[1;35m'      # magenta    — [nvidia-usb] detail lines
    _C_WARN = '\033[1;33m'     # yellow     — [warn]
    _C_FAIL = '\033[1;31m'     # red        — [fail]

    def _colorize(self, color, text):
        if not sys.stdout.isatty():
            return text
        return '%s%s%s' % (color, text, self._C_RESET)

    def _log(self, message):
        print('%s %s' % (self._colorize(self._C_LOG, '[nvidia-usb]'), message))

    def _warn(self, message):
        print('%s %s' % (self._colorize(self._C_WARN, '[warn]'), message), file=sys.stderr)

    def _die(self, message):
        raise RuntimeError('%s %s' % (self._colorize(self._C_FAIL, '[fail]'), message))

    def _banner(self, name):
        print('\n%s' % self._colorize(self._C_BANNER, '==> %s' % name))

    @contextmanager
    def _step(self, name):
        """Print a phase banner around a group of build steps.

        Args:
            name: The phase name to print in the banner.

        Yields:
            None.
        """
        self._banner(name)
        started = time.time()
        yield
        if self.verbose:
            self._log('%s done (%.1fs)' % (name, time.time() - started))

    # -------------------------------------------------------- running commands
    # Deliberately NOT shell_utils.run_command here: it unconditionally
    # prints a command's stdout and stderr (even when both are empty), which
    # is what produced the wall of blank lines and scary-looking tracebacks
    # for perfectly routine, expected-to-sometimes-fail checks (mountpoint
    # -q, readelf on a non-ELF file, etc). This stays quiet unless a command
    # both fails AND was expected to succeed (check=True, the default), or
    # --nvidia_verbose was passed.
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

        Examples: mountpoint -q on an unmounted path, umount on something
        never mounted, readelf on a non-ELF file — never warns on
        failure.

        Args:
            command: Argv list to execute.

        Returns:
            The completed subprocess.CompletedProcess, or None if the
            command's executable could not be found.
        """
        return self._run(command, check=False)

    # ------------------------------------------------------------ checks
    def check_running_as_root(self):
        """Verify the process is running as root.

        Raises:
            RuntimeError: If not running as root.
        """
        if os.geteuid() != 0:
            self._die('Run as root (sudo).')

    def check_required_tools(self):
        """Verify every tool in REQUIRED_TOOLS is on PATH.

        Raises:
            RuntimeError: If any required tool is missing.
        """
        missing = [tool for tool in self.REQUIRED_TOOLS if shutil.which(tool) is None]
        if missing:
            self._die('Missing host tool(s): %s' % ', '.join(missing))

    def validate_driver_spec(self):
        """Verify self.driver_spec is 'latest' or a valid version prefix.

        Raises:
            RuntimeError: If driver_spec is neither 'latest' nor a
                version-prefix-shaped string.
        """
        if self.driver_spec == 'latest':
            return
        if not re.fullmatch(r'[0-9]+(\.[0-9]+)*(-[0-9]+)?', self.driver_spec):
            self._die(
                "--driver takes 'latest' or a version prefix like 580 / "
                '580.105.08 / 580.105.08-4'
            )

    def resolve_image_path(self):
        """Resolve self.image_path (auto-detecting if not given) and out_image.

        Raises:
            RuntimeError: If no image is given and none/multiple *.img
                files are found alongside this module, the image does
                not exist, or the image looks already patched.
        """
        if self.image_path:
            image = Path(self.image_path)
        else:
            script_dir = Path(__file__).resolve().parent
            candidates = sorted(
                p for p in script_dir.glob('*.img') if '-nvidia' not in p.name
            )
            if len(candidates) == 0:
                self._die(
                    'No image given and no *.img found in %s. '
                    'Pass image_path explicitly.' % script_dir
                )
            if len(candidates) > 1:
                self._die(
                    'Multiple images in %s — pass one explicitly: %s'
                    % (script_dir, ', '.join(str(c) for c in candidates))
                )
            image = candidates[0]
            self._log('Auto-detected image: %s' % image)

        if not image.is_file():
            self._die('Image not found: %s' % image)
        if '-nvidia' in image.name:
            self._die(
                'Input looks like an already-patched image — start from the '
                'clean repair image.'
            )

        self.image_path = image.resolve()
        if self.output_path is not None:
            self.out_image = self.output_path
        else:
            # Default: same filename as the input image, with "-nvidia"
            # appended ahead of the extension.
            self.out_image = self.image_path.with_name(
                self.image_path.stem + '-nvidia' + self.image_path.suffix
            )
        if self.out_image.exists():
            self._warn('Removing previous output %s' % self.out_image)
            self.out_image.unlink()

    # Size of the loopback filesystem used to hold the overlay build's
    # upper/work dirs (see _prepare_build_filesystem below).
    BUILD_FS_SIZE_GB = 8

    def prepare_workdir(self):
        """Create the build working directory tree and package downloader.

        Also clears any stale mounts left over from a previous, aborted
        run at the same workdir.
        """
        if self.workdir is None:
            self.workdir = self.out_image.parent / 'nvidia-usb-work'
        self.mnt = self.workdir / 'mnt'
        self.efimnt = self.workdir / 'efi'
        self.homemnt = self.workdir / 'home'
        self.merged = self.workdir / 'merged'
        # self.upper / self.ovlwork are NOT created here — they live on a
        # dedicated loopback filesystem set up by _prepare_build_filesystem()
        # right before the overlay chroot is mounted. See that method for why.
        for path in (self.mnt, self.efimnt, self.homemnt, self.merged):
            path.mkdir(parents=True, exist_ok=True)

        # Package downloads (pin_pkg()/fetch_pins() below) go through the
        # shared PackageDownloader so this project has exactly one
        # hardened download implementation (atomic .part-then-rename,
        # cache-aware) rather than one per tool.
        self._downloader = PackageDownloader(
            self.workdir / 'pkgs', verbose=self.verbose,
            log=self._log, warn=self._warn,
        )

        for mountpoint in (self.merged, self.efimnt, self.homemnt, self.mnt):
            if self._is_mountpoint(mountpoint):
                self._warn('Stale mount from a previous run at %s — unmounting' % mountpoint)
                self._run(['umount', '-R', str(mountpoint)])

    def _is_mountpoint(self, path):
        """Check whether path is currently a mountpoint.

        Args:
            path: Path to check.

        Returns:
            True if path is a mountpoint, False otherwise.
        """
        result = self._run_quiet(['mountpoint', '-q', str(path)])
        return result is not None and result.returncode == 0

    def _prepare_build_filesystem(self):
        """Create/mount the loopback ext4 filesystem backing the overlay build.

        overlayfs refuses to use a case-insensitive-capable filesystem as
        an upperdir — and SteamOS's own /home partition (where this
        project's workdir lives by default, since the output image is
        normally saved alongside SteamOS-Utils under /home/deck/) is
        ext4 with casefold enabled. The original bash tool already hit
        this on-device (see its repatch.py, which builds inside a
        loopback ext4 image on /home for exactly this reason) — apply
        the same fix here for the main build.

        Only the overlay's upper + work dirs need to live on this plain
        filesystem; the overlay mount TARGET (self.merged) is just an
        empty directory and is unaffected, so it stays wherever workdir
        is.
        """
        self.buildfs_img = self.workdir / '.build-fs.img'
        self.buildfs_mnt = self.workdir / 'buildfs'
        self.buildfs_mnt.mkdir(parents=True, exist_ok=True)

        if not self._is_mountpoint(self.buildfs_mnt):
            if not self.buildfs_img.exists():
                self._log(
                    'Creating build filesystem image (%s, %dG) — works around '
                    "SteamOS's casefold /home rejecting overlayfs upperdirs"
                    % (self.buildfs_img, self.BUILD_FS_SIZE_GB)
                )
                self._run(['truncate', '-s', '%dG' % self.BUILD_FS_SIZE_GB, str(self.buildfs_img)])
                self._run(['mkfs.ext4', '-q', '-F', str(self.buildfs_img)])
            self._run(['mount', '-o', 'loop', str(self.buildfs_img), str(self.buildfs_mnt)])

        self.upper = self.buildfs_mnt / 'upper'
        self.ovlwork = self.buildfs_mnt / 'ovlwork'
        self.upper.mkdir(parents=True, exist_ok=True)
        self.ovlwork.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------- image copy
    def copy_image(self):
        """Copy the input image to out_image (reflink where supported)."""
        self._log('Copying image -> %s' % self.out_image)
        self._run(['cp', '--reflink=auto', str(self.image_path), str(self.out_image)])

    def loop_mount(self):
        """Attach out_image as a loop device with partition scanning."""
        result = self._run(['losetup', '-f', '--show', '-P', str(self.out_image)])
        self.loopdev = result.stdout.strip() if result and result.stdout else '/dev/loopX'
        self._log('Loop device: %s' % self.loopdev)

    # Partition-name variants seen across SteamOS image layouts: dual-slot
    # images label these rootfs-A/efi-A (slot A of an A/B pair); some
    # single-slot OOBE repair images instead just use the bare name. Either
    # is acceptable; prefer the "-A" form when both happen to be present by
    # trying it first.
    ROOT_PART_NAMES = ('rootfs-A', 'rootfs')
    EFI_PART_NAMES = ('efi-A', 'efi')
    HOME_PART_NAMES = ('home',)

    def discover_partitions(self):
        """Find and mount the root/efi/home partitions on the loop device.

        Raises:
            RuntimeError: If any of the root/efi/home partitions cannot
                be found.
        """
        found = {}
        candidates = sorted(Path('/dev').glob(os.path.basename(self.loopdev) + 'p*'))
        for part in candidates:
            result = self._run(['blkid', '-p', '-s', 'PART_ENTRY_NAME', '-o', 'value', str(part)])
            name = result.stdout.strip() if result and result.stdout else ''
            if name:
                found.setdefault(name, part)

        def pick(names):
            for name in names:
                if name in found:
                    return found[name]
            return None

        self.root_part = pick(self.ROOT_PART_NAMES)
        self.efi_part = pick(self.EFI_PART_NAMES)
        self.home_part = pick(self.HOME_PART_NAMES)

        if not (self.root_part and self.efi_part and self.home_part):
            seen = ', '.join(sorted(found)) if found else '(none)'
            self._die(
                'Could not find root/efi/home partitions (saw: %s) — is this a SteamOS image?' % seen
            )

        # Standard SteamOS images ship rootfs as btrfs (hence compress-force
        # + the read-only subvolume property); some repair/recovery image
        # variants use a plain ext4 rootfs instead. Detect it rather than
        # assume, so the mount options and the ro-property dance only apply
        # when they're actually meaningful.
        self.root_fs_type = self._detect_fs_type(self.root_part)
        self._log('Root partition filesystem: %s' % (self.root_fs_type or 'unknown'))

        if self.root_fs_type == 'btrfs':
            self._run(['mount', '-o', 'compress-force=zstd:3', str(self.root_part), str(self.mnt)])
        else:
            self._run(['mount', str(self.root_part), str(self.mnt)])
        self._run(['mount', str(self.efi_part), str(self.efimnt)])
        self._run(['mount', str(self.home_part), str(self.homemnt)])

        if self.root_fs_type == 'btrfs':
            result = self._run(['btrfs', 'property', 'get', str(self.mnt), 'ro'])
            if result and result.stdout and 'ro=true' in result.stdout:
                self._log('Clearing btrfs read-only property')
                self._run(['btrfs', 'property', 'set', str(self.mnt), 'ro', 'false'])

    def _detect_fs_type(self, part):
        """Detect the filesystem type of a block device partition.

        Args:
            part: Path to the partition device, or None.

        Returns:
            The detected filesystem type string, or None if part is
            None or the type could not be detected.
        """
        if part is None:
            return None
        result = self._run_quiet(['blkid', '-p', '-s', 'TYPE', '-o', 'value', str(part)])
        return result.stdout.strip() if result and result.returncode == 0 and result.stdout else None

    # Kernel-version directory names look like "6.11.0-valve10-1-neptune" —
    # matched case-insensitively against "neptune" first (the expected,
    # documented case), falling back to "any directory that looks like a
    # kernel version" in case a particular image variant names it
    # differently, before giving up.
    _KVER_FALLBACK_RE = re.compile(r'^\d+\.\d+(\.\d+)?-')

    def discover_kernel_version(self):
        """Find the SteamOS/neptune kernel version directory in the image.

        Raises:
            RuntimeError: If no neptune (or neptune-like) kernel module
                directory can be found.
        """
        modules_dir = self.mnt / 'usr' / 'lib' / 'modules'
        self.kernel_version = None

        entries = []
        if modules_dir.exists():
            entries = sorted(p for p in modules_dir.iterdir() if p.is_dir())
            for entry in entries:
                if 'neptune' in entry.name.lower():
                    self.kernel_version = entry.name
                    break
            if not self.kernel_version:
                for entry in entries:
                    if self._KVER_FALLBACK_RE.match(entry.name):
                        self.kernel_version = entry.name
                        self._warn(
                            '"%s" has no "neptune" in its name — using it anyway '
                            'as the only kernel-version-looking directory found' % entry.name
                        )
                        break

        if not self.kernel_version:
            if not modules_dir.exists():
                top_level = sorted(p.name for p in self.mnt.iterdir()) if self.mnt.exists() else []
                self._warn('Top-level contents of mounted rootfs (%s): %s' % (
                    self.mnt, ', '.join(top_level) or '(empty)'
                ))
                self._die(
                    '%s does not exist in the mounted image — the "rootfs" partition '
                    'may not be the actual SteamOS root filesystem (see directory '
                    'listing above)' % modules_dir
                )
            self._warn('Contents of %s: %s' % (
                modules_dir, ', '.join(e.name for e in entries) or '(empty)'
            ))
            self._die(
                'No neptune (or neptune-like) kernel module directory found under %s '
                '— see the directory listing above' % modules_dir
            )
        self._log('Image kernel: %s' % self.kernel_version)

    def resolve_headers_url(self):
        """Resolve the exact-match kernel headers package URL for the image.

        Reads the mounted image's own pacman.conf/mirrorlist to build
        the jupiter-repo + $repo/$arch mirror-template URL for the
        headers package matching the image's installed kernel package.

        Raises:
            RuntimeError: If the installed kernel package, jupiter repo,
                mirror server, or exact-match headers package cannot be
                resolved/found.
        """
        pacman_conf = self.mnt / 'etc' / 'pacman.conf'
        mirrorlist = self.mnt / 'etc' / 'pacman.d' / 'mirrorlist'
        pacdb = self.mnt / 'usr' / 'lib' / 'holo' / 'pacmandb' / 'local'

        kpkg_dir = None
        if pacdb.exists():
            for entry in sorted(pacdb.glob('linux-neptune-*')):
                if not entry.is_dir():
                    continue
                if any(tag in entry.name for tag in ('-headers-', 'firmware', 'rtw')):
                    continue
                kpkg_dir = entry
                break
        if not kpkg_dir:
            self._die('Could not find installed kernel package in pacman db')

        if kpkg_dir:
            kpkg_full = kpkg_dir.name
            kpkg_name, _, rest = kpkg_full.rpartition('-')
            kpkg_name, _, verrel_head = kpkg_name.rpartition('-')
            kpkg_name = kpkg_full.rsplit('-', 2)[0]
            kpkg_verrel = kpkg_full[len(kpkg_name) + 1:]
        else:
            kpkg_name, kpkg_verrel = 'linux-neptune', '0-0'
        self._log('Kernel package: %s %s' % (kpkg_name, kpkg_verrel))

        jupiter_repo = None
        if pacman_conf.exists():
            match = re.search(r'^\[(jupiter-[^\]]+)\]', pacman_conf.read_text(errors='ignore'), re.M)
            jupiter_repo = match.group(1) if match else None
        if not jupiter_repo:
            self._die('No jupiter repo in image pacman.conf')

        mirror = None
        if mirrorlist.exists():
            match = re.search(r'^Server\s*=\s*(\S+)', mirrorlist.read_text(errors='ignore'), re.M)
            mirror = match.group(1) if match else None
        if not mirror:
            self._die('No mirror server found in image mirrorlist')

        if mirror and jupiter_repo:
            hdr_url = mirror.replace('$repo', jupiter_repo).replace('$arch', 'x86_64')
            hdr_url = '%s/%s-headers-%s-x86_64.pkg.tar.zst' % (hdr_url, kpkg_name, kpkg_verrel)
        else:
            hdr_url = None

        if hdr_url and not self._url_exists(hdr_url):
            self._die("Exact-match headers not found in Valve's pool: %s" % hdr_url)

        self.headers_url = hdr_url
        self._log('Headers package: %s' % os.path.basename(hdr_url))

    # -------------------------------------------------- Arch package pins
    def _url_exists(self, url):
        """Check whether url responds with a 2xx status.

        Args:
            url: The URL to probe.

        Returns:
            True if url exists, False otherwise.
        """
        return self._downloader.url_exists(url)

    def pin_pkg(self, pkg, spec):
        """Resolve one Arch package + version to a permanently pinned URL.

        Appends the resolved URL/filename to self.pkg_urls/self.pkg_files.

        Args:
            pkg: The Arch package name to resolve.
            spec: 'latest', or a version prefix to match against the
                Arch archive listing for pkg.

        Returns:
            The resolved version string, or None if resolution failed
            (in which case _die() has already raised).

        Raises:
            RuntimeError: If pkg cannot be resolved via archlinux.org
                (for 'latest') or found in the Arch archive matching
                spec, or if neither archive.archlinux.org nor the
                pkgbuild.com mirror has the resolved build.
        """
        if spec == 'latest':
            search_url = 'https://archlinux.org/packages/search/json/?name=%s' % pkg
            try:
                with urllib.request.urlopen(search_url) as resp:
                    data = json.load(resp)
            except Exception as exc:
                self._die('Could not resolve %s from archlinux.org: %s' % (pkg, exc))
                return

            results = [
                r for r in data.get('results', [])
                if r.get('repo') in ('core', 'extra', 'multilib') and r.get('arch') == 'x86_64'
            ]
            if not results:
                self._die('No published build of %s found on archlinux.org' % pkg)
                return
            record = results[0]
            ver = '%s-%s' % (record['pkgver'], record['pkgrel'])
            filename = record['filename']
            repo = record['repo']

            url = '%s/%s/%s/%s' % (self.ARCHIVE_URL, pkg[0], pkg, filename)
            if not self._url_exists(url):
                url = 'https://geo.mirror.pkgbuild.com/%s/os/x86_64/%s' % (repo, filename)
                if not self._url_exists(url):
                    self._die('%s %s not on archive.archlinux.org nor the mirror' % (pkg, ver))
                self._warn('%s not yet in the Arch archive — pinning mirror URL (may go stale)' % pkg)
        else:
            listing_url = '%s/%s/%s/' % (self.ARCHIVE_URL, pkg[0], pkg)
            try:
                with urllib.request.urlopen(listing_url) as resp:
                    html = resp.read().decode('utf-8', errors='ignore')
            except Exception as exc:
                self._die('Could not list Arch archive for %s: %s' % (pkg, exc))
                return

            pattern = re.compile(
                r'%s-%s[.\-][^"<]*-x86_64\.pkg\.tar\.zst' % (re.escape(pkg), re.escape(spec))
            )
            matches = sorted(set(pattern.findall(html)))
            if not matches:
                self._die(
                    "No %s build matching '%s' in the Arch archive "
                    '(bad driver spec, or no network)' % (pkg, spec)
                )
                return
            filename = matches[-1]
            ver = filename[len(pkg) + 1:-len('-x86_64.pkg.tar.zst')]
            url = '%s/%s/%s/%s' % (self.ARCHIVE_URL, pkg[0], pkg, filename)

        self.pkg_urls.append(url)
        self.pkg_files.append(filename)
        self._log('  %s %s' % (pkg, ver))
        return ver

    def fetch_pins(self):
        """Download every URL currently pinned in self.pkg_urls/pkg_files.

        Raises:
            RuntimeError: If any download fails.
        """
        for url, filename in zip(self.pkg_urls, self.pkg_files):
            try:
                self._downloader.download(url, filename)
            except RuntimeError as exc:
                self._die(str(exc))

    def resolve_driver_packages(self):
        """Pin and download the NVIDIA driver package set from Arch Linux.

        Resolves nvidia-utils first (its dependency list decides which
        companion packages apply), then nvidia-open-dkms and
        lib32-nvidia-utils, then any Arch-only dependency (e.g.
        egl-wayland2) that Valve's frozen mirror predates.

        Raises:
            RuntimeError: If any package cannot be resolved, or a
                companion package's version doesn't match nvidia-utils'
                (mirror mid-update).
        """
        self._log('Resolving NVIDIA driver packages from Arch Linux (--driver %s)' % self.driver_spec)
        ver = self.pin_pkg('nvidia-utils', self.driver_spec)
        self.driver_version = ver
        self.nv_pkgver = ver.rsplit('-', 1)[0] if ver else None
        self._log('Driver pinned: nvidia-open %s' % self.driver_version)

        # nvidia-utils first: its dependency list decides which companions apply.
        self.fetch_pins()

        companion_spec = self.driver_spec if self.driver_spec != 'latest' else 'latest'
        if self.driver_spec == 'latest':
            companion_spec = 'latest'
        else:
            companion_spec = self.nv_pkgver

        for pkg in ('nvidia-open-dkms', 'lib32-nvidia-utils'):
            pin_ver = self.pin_pkg(pkg, companion_spec)
            if pin_ver and self.nv_pkgver and not pin_ver.startswith(self.nv_pkgver + '-'):
                self._die(
                    'Version skew: %s is %s but nvidia-utils is %s '
                    '(mirror mid-update?) — retry in an hour' % (pkg, pin_ver, self.driver_version)
                )

        # Any dependency of nvidia-utils that Valve's frozen repo predates.
        for dep in self._read_arch_only_deps():
            if dep in self.ARCH_ONLY_DEPS:
                self._log('  %s also needs %s, which Valve\'s repo predates' % (self.driver_version, dep))
                self.pin_pkg(dep, 'latest')

        self.fetch_pins()

    def _read_arch_only_deps(self):
        """Read the dependency list out of the pinned nvidia-utils package.

        Returns:
            A list of dependency package names (version constraints
            stripped), or an empty list if the package file isn't
            available yet.
        """
        if not self.pkg_files:
            return []
        pkg_dir = self.workdir / 'pkgs'
        pkgfile = pkg_dir / self.pkg_files[0]
        if not pkgfile.exists():
            return []
        result = self._run(['tar', '-xOf', str(pkgfile), '.PKGINFO'])
        if not result or not result.stdout:
            return []
        deps = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == 'depend':
                deps.append(re.sub(r'[<>=].*', '', parts[2]))
        return deps

    # --------------------------------------------------- glibc compatibility
    def check_glibc_compatibility(self):
        """Verify the pinned driver payload's glibc requirement fits the image.

        Extracts every pinned package and scans its ELF binaries/shared
        libraries for the highest GLIBC_x.y symbol version referenced,
        then compares that against the image's own glibc version.

        Raises:
            RuntimeError: If the image's glibc version cannot be
                determined, or the payload needs a newer glibc than the
                image ships (current Arch has drifted too far ahead of
                Valve's frozen image).
        """
        pacdb = self.mnt / 'usr' / 'lib' / 'holo' / 'pacmandb' / 'local'
        img_glibc = None
        if pacdb.exists():
            for entry in pacdb.glob('glibc-[0-9]*'):
                match = re.match(r'glibc-(\d+\.\d+)', entry.name)
                if match:
                    img_glibc = match.group(1)
                    break
        if not img_glibc:
            self._die('Could not determine image glibc version')
        self._log('Checking payload glibc requirements against image glibc %s' % img_glibc)

        scan_dir = Path(tempfile.mkdtemp(prefix='glibc-scan-', dir=str(self.workdir)))
        pkg_dir = self.workdir / 'pkgs'
        max_glibc = '0.0'
        try:
            for filename in self.pkg_files:
                target = scan_dir / filename.replace('.pkg.tar.zst', '')
                target.mkdir(parents=True, exist_ok=True)
                self._run(['tar', '-xf', str(pkg_dir / filename), '-C', str(target)])

            versions = set()
            for path in scan_dir.rglob('*'):
                if not path.is_file():
                    continue
                if not (path.suffix.startswith('.so') or '.so' in path.name or os.access(path, os.X_OK)):
                    continue
                result = self._run_quiet(['readelf', '-V', str(path)])
                if result and result.stdout:
                    versions.update(re.findall(r'GLIBC_([0-9.]+)', result.stdout))

            if versions:
                max_glibc = sorted(versions, key=lambda v: [int(x) for x in v.split('.')])[-1]

            def ver_tuple(v):
                return tuple(int(x) for x in v.split('.'))

            if img_glibc and ver_tuple(max_glibc) > ver_tuple(img_glibc):
                self._die(
                    'Driver payload needs glibc %s but the image only has %s — '
                    'current Arch has drifted too far; this needs the .run-installer '
                    'approach instead' % (max_glibc, img_glibc)
                )
            self._log('OK: payload needs at most glibc %s (image has %s)' % (max_glibc, img_glibc))
        finally:
            shutil.rmtree(scan_dir, ignore_errors=True)

    # ------------------------------------------------------- overlay build
    def setup_overlay_chroot(self):
        """Mount the overlay build chroot over the image's rootfs.

        Raises:
            RuntimeError: If the overlay mount fails, or the resulting
                chroot has no /bin/bash (a sign the overlay mount did
                not actually succeed).
        """
        self._prepare_build_filesystem()
        self._log('Setting up overlay build chroot (build residue stays out of the image)')
        result = self._run([
            'mount', '-t', 'overlay', 'overlay', '-o',
            'index=off,lowerdir=%s,upperdir=%s,workdir=%s' % (self.mnt, self.upper, self.ovlwork),
            str(self.merged),
        ])
        if result is not None and result.returncode != 0:
            self._die(
                'overlay mount failed — see the mount error above '
                '(a casefold-enabled upperdir filesystem is a common cause; '
                'this should be worked around automatically, so if you see '
                "that error again the build filesystem likely didn't mount)"
            )
        self._run(['mount', '-t', 'proc', 'proc', str(self.merged / 'proc')])
        self._run(['mount', '--rbind', '/sys', str(self.merged / 'sys')])
        self._run(['mount', '--make-rslave', str(self.merged / 'sys')])
        self._run(['mount', '--rbind', '/dev', str(self.merged / 'dev')])
        self._run(['mount', '--make-rslave', str(self.merged / 'dev')])
        resolv = self.merged / 'etc' / 'resolv.conf'
        self._run(['rm', '-f', str(resolv)])
        try:
            shutil.copy('/etc/resolv.conf', resolv)
        except OSError:
            pass

        # Fail fast and loud here rather than let a broken chroot
        # cascade into a dozen misleading "command failed" warnings from
        # every step downstream (pacman-key, headers, dkms, ...) that
        # all fail the same way once /bin/bash isn't reachable inside it.
        if not (self.merged / 'bin' / 'bash').exists():
            self._die(
                '%s has no /bin/bash — the overlay chroot did not mount '
                'correctly (see the mount error above)' % self.merged
            )

    def _chroot_argv(self, root, *args):
        """Build a capability-dropped chroot argv for root.

        See CHROOT_DROP_CAPS: every chroot invocation goes through this
        so nothing run inside it can load/unload a kernel module,
        reboot, or do raw device I/O against the REAL host, which shares
        one kernel with the chroot regardless of its filesystem root.

        Args:
            root: Chroot target directory.
            *args: Command and arguments to run inside the chroot.

        Returns:
            The full argv list, including CHROOT_DROP_CAPS.
        """
        return self.CHROOT_DROP_CAPS + ['chroot', str(root)] + list(args)

    def _in_chroot(self, shell_command):
        """Run shell_command inside the build overlay chroot (self.merged).

        Args:
            shell_command: The shell command line to run.

        Returns:
            The completed subprocess.CompletedProcess, or None if the
            command's executable could not be found.
        """
        return self._run(self._chroot_argv(self.merged, '/bin/bash', '-c', shell_command))

    def build_driver(self):
        """Install kernel headers, dkms, and the pinned NVIDIA driver in the chroot.

        Compiles the kernel module via dkms if the post-install hook
        doesn't already build it.
        """
        pacman_conf_path = '/etc/pacman.conf'
        if self.skip_sigcheck:
            merged_conf = self.merged / 'etc' / 'pacman.conf'
            nosig_conf = self.merged / 'tmp' / 'pacman-nosig.conf'
            if merged_conf.exists():
                text = merged_conf.read_text()
                text = re.sub(r'^SigLevel.*', 'SigLevel = Never', text, flags=re.M)
                nosig_conf.write_text(text)
            pacman_conf_path = '/tmp/pacman-nosig.conf'
            self._warn('pacman signature verification DISABLED for the build')

        keyring_dir = self.merged / 'etc' / 'pacman.d' / 'gnupg' / 'private-keys-v1.d'
        if not self.skip_sigcheck and not keyring_dir.is_dir():
            self._log('Initialising pacman keyring in chroot')
            self._in_chroot('pacman-key --init && pacman-key --populate')

        self._log('Downloading exact-match kernel headers')
        if self.headers_url:
            self._in_chroot("curl -sfL '%s' -o /tmp/headers.pkg.tar.zst" % self.headers_url)

        self._log('Refreshing pacman databases')
        self._in_chroot('pacman --config %s -Sy' % pacman_conf_path)

        self._log('Installing headers + dkms (from Valve\'s mirror)')
        self._in_chroot('pacman --config %s -U --noconfirm --needed /tmp/headers.pkg.tar.zst' % pacman_conf_path)
        self._in_chroot('pacman --config %s -S --noconfirm --needed dkms' % pacman_conf_path)

        self._log('Installing pinned Arch driver packages (compiles the module, takes a few minutes)')
        nvpkgs_dir = self.merged / 'tmp' / 'nvpkgs'
        self._run(['rm', '-rf', str(nvpkgs_dir)])
        nvpkgs_dir.mkdir(parents=True, exist_ok=True)
        pkg_dir = self.workdir / 'pkgs'
        for filename in self.pkg_files:
            self._run(['cp', str(pkg_dir / filename), str(nvpkgs_dir)])
        self._in_chroot('pacman --config %s -U --noconfirm --needed /tmp/nvpkgs/*.pkg.tar.zst' % pacman_conf_path)

        module_glob = list((self.merged / 'usr' / 'lib' / 'modules' / (self.kernel_version or '')).glob('updates/dkms/nvidia.ko*')) \
            if self.kernel_version else []
        if not module_glob:
            self._log("DKMS hook didn't build for %s — forcing" % self.kernel_version)
            self._in_chroot('dkms autoinstall -k %s' % self.kernel_version)

        result = self._in_chroot('pacman -Q nvidia-utils')
        self.driver_version_installed = (
            result.stdout.split()[1] if result and result.stdout and len(result.stdout.split()) > 1 else self.driver_version
        )
        self._log('Built nvidia-open %s for %s' % (self.driver_version_installed, self.kernel_version))

    # ----------------------------------------------------------- payload
    def compute_payload(self):
        """Diff pre/post-build package lists to compute the driver payload.

        Excludes build-only packages (BUILD_ONLY_RE) and, if
        self.trim_cuda, CUDA/OpenCL/NVVM/OptiX files. Writes the
        resulting relative file list to self.filelist_rel.

        Raises:
            RuntimeError: If the resulting payload package list is
                empty.
        """
        before_file = self.workdir / 'pkgs-before.txt'
        after_file = self.workdir / 'pkgs-after.txt'

        before = self._run(['pacman', '-Qq', '--dbpath', str(self.mnt / 'usr' / 'lib' / 'holo' / 'pacmandb')])
        before_file.write_text((before.stdout if before and before.stdout else ''))

        after = self._in_chroot('pacman -Qq')
        after_file.write_text((after.stdout if after and after.stdout else ''))

        before_set = set(l for l in before_file.read_text().splitlines() if l)
        after_set = set(l for l in after_file.read_text().splitlines() if l)
        new_pkgs = sorted(after_set - before_set)
        self.new_packages = [p for p in new_pkgs if not self.BUILD_ONLY_RE.match(p)]

        if not self.new_packages:
            self._die('Payload package list came out empty — check %s' % self.workdir)
        self._log('Payload packages: %s' % ' '.join(self.new_packages))

        filelist = self.workdir / 'payload-files.txt'
        lines = []
        for pkg in self.new_packages:
            result = self._in_chroot('pacman -Qlq %s' % pkg)
            if result and result.stdout:
                lines.extend(result.stdout.splitlines())
        filelist.write_text('\n'.join(lines) + ('\n' if lines else ''))

        if self.trim_cuda:
            self._log('Trimming CUDA/OpenCL/NVVM/OptiX libraries')
            pattern = re.compile(
                r'libcuda|libcudadebugger|libnvidia-nvvm|libnvidia-opencl|libnvoptix|nvidia-cuda-mps|OpenCL'
            )
            kept = [l for l in filelist.read_text().splitlines() if not pattern.search(l)]
            filelist.write_text('\n'.join(kept) + ('\n' if kept else ''))

        rel_file = filelist.with_suffix('.txt.rel')
        rel_lines = [l.lstrip('/') for l in filelist.read_text().splitlines()]
        rel_file.write_text('\n'.join(rel_lines) + ('\n' if rel_lines else ''))
        self.filelist_rel = rel_file

    def install_payload(self):
        """Copy the computed driver payload into the image rootfs.

        Registers the new packages in the image's own pacman db and
        runs depmod + ldconfig against the patched rootfs.
        """
        self._log('Copying driver payload into the image rootfs')
        self._run(['rsync', '-a', '--files-from=%s' % self.filelist_rel, str(self.merged) + '/', str(self.mnt) + '/'])
        if self.kernel_version:
            self._run([
                'rsync', '-a',
                str(self.upper / 'usr' / 'lib' / 'modules' / self.kernel_version / 'updates'),
                str(self.mnt / 'usr' / 'lib' / 'modules' / self.kernel_version) + '/',
            ])

        self._log("Registering payload packages in the image's pacman db")
        local_db = self.upper / 'usr' / 'lib' / 'holo' / 'pacmandb' / 'local'
        dest_db = self.mnt / 'usr' / 'lib' / 'holo' / 'pacmandb' / 'local'
        for pkg in self.new_packages:
            for entry in sorted(local_db.glob('%s-[0-9]*' % pkg)) if local_db.exists() else []:
                if entry.is_dir():
                    self._run(['rsync', '-a', str(entry), str(dest_db) + '/'])
                    break

        self._log('Running depmod + ldconfig in the image')
        if self.kernel_version:
            self._run(self._chroot_argv(self.mnt, 'depmod', self.kernel_version))
        self._run(self._chroot_argv(self.mnt, 'ldconfig'))

    def configure_modprobe(self):
        """Blacklist nouveau, enable nvidia KMS, and enable suspend/resume units."""
        self._log('Writing modprobe config (blacklist nouveau, enable nvidia KMS)')
        conf = self.mnt / 'etc' / 'modprobe.d' / '99-nvidia-patch.conf'
        content = (
            '# Added by NvidiaUsbImageBuilder\n'
            'blacklist nouveau\n'
            'options nouveau modeset=0\n'
            'options nvidia-drm modeset=1 fbdev=1\n'
            'options nvidia NVreg_PreserveVideoMemoryAllocations=1\n'
        )
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(content)

        self._log('Enabling nvidia suspend/resume services')
        self._run(self._chroot_argv(self.mnt, 'systemctl', 'enable',
                                     'nvidia-suspend', 'nvidia-resume', 'nvidia-hibernate'))

    # ------------------------------------------------------ update modes
    def configure_update_strategy(self):
        """Configure the image's update-mode behavior (selfheal/hold/stock).

        Masks the OOBE-migration service for non-stock modes, then
        dispatches to _configure_hold_updates() or
        _configure_selfheal_updates() as appropriate.
        """
        oobe_service = self.mnt / 'usr' / 'lib' / 'systemd' / 'system' / 'steamos-finish-oobe-migration.service'
        if self.update_mode != 'stock' and oobe_service.exists():
            link = self.mnt / 'etc' / 'systemd' / 'system' / 'steamos-finish-oobe-migration.service'
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to('/dev/null')

        if self.update_mode == 'hold':
            self._configure_hold_updates()
        elif self.update_mode == 'selfheal':
            self._configure_selfheal_updates()
        else:
            self._log('Update mode: stock (an OS update will remove the NVIDIA driver!)')

    def _configure_hold_updates(self):
        """Mask the updater services and stub the update CLIs (hold mode)."""
        self._log('Holding OS updates: masking updater services, stubbing CLIs')
        atomupd_service = self.mnt / 'usr' / 'lib' / 'systemd' / 'system' / 'atomupd.service'
        if atomupd_service.exists():
            link = self.mnt / 'etc' / 'systemd' / 'system' / 'atomupd.service'
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to('/dev/null')

        stub = (
            '#!/bin/bash\n'
            '# Stubbed by NvidiaUsbImageBuilder: an OS update would replace the\n'
            '# rootfs and remove the NVIDIA driver. Original saved as $0.orig.\n'
            'echo "OS updates are held on this system (NVIDIA-patched image)." >&2\n'
            'exit 7\n'
        )
        for name in ('steamos-update', 'steamos-update-os', 'steamos-atomupd-client'):
            binpath = self.mnt / 'usr' / 'bin' / name
            origpath = self.mnt / 'usr' / 'bin' / (name + '.orig')
            if binpath.exists() and not origpath.exists():
                shutil.move(str(binpath), str(origpath))
                binpath.write_text(stub)
                binpath.chmod(0o755)

    def _configure_selfheal_updates(self):
        """Install the self-healing update machinery (selfheal mode).

        Writes driver.json, copies repatch.py onto the image, and wraps
        every update entry point that actually exists in the image.

        Raises:
            RuntimeError: If repatch_script.py is missing from this
                project's own install (should ship next to
                nvidia_usb_image_builder.py).
        """
        self._log('Installing self-healing update machinery')
        lib_dir = self.mnt / 'usr' / 'lib' / 'steamos-nvidia'
        lib_dir.mkdir(parents=True, exist_ok=True)

        # JSON, not a bash-sourceable driver.conf: repatch.py (below) is
        # now pure Python, so there's no reason to keep shipping a
        # shell-format config for it to `source`.
        (lib_dir / 'driver.json').write_text(json.dumps({
            'comment': 'Written by NvidiaUsbImageBuilder at image build time. '
                       'repatch.py installs the driver from these pinned URLs.',
            'driver_spec': self.driver_spec,
            'driver_version': self.driver_version,
            'pkg_urls': list(self.pkg_urls),
        }, indent=2) + '\n')

        if not self.REPATCH_SCRIPT.exists():
            self._die('%s is missing — reinstall/redownload SteamOS-Utils '
                       '(repatch_script.py should ship next to nvidia_usb_image_builder.py)'
                       % self.REPATCH_SCRIPT)

        repatch_path = lib_dir / 'repatch.py'
        shutil.copy(self.REPATCH_SCRIPT, repatch_path)
        repatch_path.chmod(0o755)

        # An OS update can be triggered through more than one entry
        # point Valve ships — the desktop-mode `steamos-update` CLI,
        # `steamos-update-os`, or Game Mode's `steamos-atomupd-client`
        # — and _configure_hold_updates() above already defends all
        # three for exactly that reason. An EARLIER version of this
        # method only wrapped `steamos-update`, which meant an update
        # triggered via one of the other two (confirmed: switching
        # update BRANCHES, e.g. stable -> main, on real hardware) never
        # ran the self-heal machinery at all — the newly staged slot
        # got no NVIDIA driver and no safety net, and the system
        # couldn't reach the UI. Wrap every plausible entry point that
        # actually exists in the image; steamos-update is wrapped
        # unconditionally (Steam Deck/SteamOS always ships it), the
        # other two only if present. repatch.py is idempotent (exits
        # immediately if the target slot already has the driver for
        # its kernel), so if more than one entry point ends up calling
        # into the same real update, running it more than once is
        # harmless.
        for name in ('steamos-update', 'steamos-update-os', 'steamos-atomupd-client'):
            binpath = self.mnt / 'usr' / 'bin' / name
            if name != 'steamos-update' and not binpath.exists():
                continue
            origpath = self.mnt / 'usr' / 'bin' / (name + '.orig')
            if not origpath.exists() and binpath.exists():
                shutil.move(str(binpath), str(origpath))
            binpath.write_text(self._update_wrapper_builder.render(name))
            binpath.chmod(0o755)

    # ---------------------------------------------------------- cmdline
    def patch_kernel_cmdline(self):
        """Append CMDLINE_ADD to the image's grub.cfg and default/grub.

        Raises:
            RuntimeError: If grub.cfg exists but the expected cmdline
                pattern isn't found in it.
        """
        self._log('Appending to kernel cmdline: %s' % self.CMDLINE_ADD)
        grub_cfg = self.efimnt / 'EFI' / 'steamos' / 'grub.cfg'
        if grub_cfg.exists():
            text = grub_cfg.read_text()
            new_text, count = re.subn(
                r'(steamenv_boot\s+linux\s+/boot/vmlinuz[^\n]*)',
                lambda m: m.group(1) + ' ' + self.CMDLINE_ADD,
                text,
            )
            if count == 0:
                self._die('grub.cfg edit failed — cmdline pattern not found')
            grub_cfg.write_text(new_text)

        default_grub = self.mnt / 'etc' / 'default' / 'grub'
        if default_grub.exists():
            text = default_grub.read_text()
            text = re.sub(
                r'^(GRUB_CMDLINE_LINUX_DEFAULT=")',
                r'\1' + self.CMDLINE_ADD + ' ',
                text,
                flags=re.M,
            )
            default_grub.write_text(text)

    # --------------------------------------------------- one-click install
    def install_one_click_installer(self):
        """Install the one-click USB installer (disk picker + desktop icons).

        Patches Valve's repair_device.sh to accept a generic (non-NVMe)
        target disk, then installs install_to_hd.sh and the desktop
        launcher icons.

        Raises:
            RuntimeError: If install_to_hd.sh is missing from this
                project's own install, the image has no
                repair_device.sh (not an OOBE repair image), or the
                repair_device.sh patch fails to apply.
        """
        if not self.add_installer:
            return

        if not self.INSTALL_TO_HD_SCRIPT.exists():
            self._die('%s is missing — reinstall/redownload SteamOS-Utils '
                       '(install_to_hd.sh should ship next to nvidia_usb_image_builder.py)'
                       % self.INSTALL_TO_HD_SCRIPT)

        tools_dir = self.homemnt / 'deck' / 'tools'
        desktop_dir = self.homemnt / 'deck' / 'Desktop'
        repair_script = tools_dir / 'repair_device.sh'
        if not repair_script.exists():
            self._die('No repair_device.sh in image home — is this the OOBE *repair* image?')

        self._log("Patching Valve's repair_device.sh for generic hardware")
        stock_copy = tools_dir / 'repair_device.sh.stock'
        shutil.copy(repair_script, stock_copy)
        text = repair_script.read_text()
        text = text.replace(
            'DISK=/dev/nvme0n1',
            'DISK="${STEAMOS_TARGET_DISK:-/dev/nvme0n1}"',
        )
        text = text.replace(
            'DISK_SUFFIX=p',
            'DISK_SUFFIX=""; [[ "$DISK" =~ [0-9]$ ]] && DISK_SUFFIX="p"',
        )
        text = re.sub(
            r'(?m)^  sanitize_all$',
            '  if [[ "$DISK" == /dev/nvme* ]]; then sanitize_all; '
            'else ewarn "Non-NVMe target: skipping NVMe sanitize"; fi',
            text,
        )
        repair_script.write_text(text)
        if 'STEAMOS_TARGET_DISK' not in repair_script.read_text():
            self._die('DISK patch failed')

        self._log('Installing disk-picker wrapper + desktop icons')
        installer_path = tools_dir / 'install_to_hd.sh'
        shutil.copy(self.INSTALL_TO_HD_SCRIPT, installer_path)
        installer_path.chmod(0o755)

        desktop_dir.mkdir(parents=True, exist_ok=True)
        (desktop_dir / 'Install SteamOS NVIDIA.desktop').write_text(self._desktop_entry(
            name='Install SteamOS (NVIDIA) to Hard Drive',
            comment='Erase an internal disk and install this NVIDIA-patched SteamOS onto it',
            exec_args='all',
            icon='drive-harddisk',
        ))
        (desktop_dir / 'Upgrade SteamOS NVIDIA.desktop').write_text(self._desktop_entry(
            name='Upgrade SteamOS (NVIDIA) — keeps games & data',
            comment='Reinstall the OS partitions from this USB while preserving the home partition',
            exec_args='system',
            icon='system-software-update',
        ))
        for entry in (desktop_dir / 'Install SteamOS NVIDIA.desktop',
                      desktop_dir / 'Upgrade SteamOS NVIDIA.desktop'):
            entry.chmod(0o755)

        self._run(['chown', '-R', '1000:1000', str(tools_dir), str(desktop_dir)])

        self._log('Adding NOPASSWD sudoers drop-in for deck (needed by the install icon)')
        sudoers = self.mnt / 'etc' / 'sudoers.d' / 'zz-deck-nopasswd'
        sudoers.parent.mkdir(parents=True, exist_ok=True)
        sudoers.write_text('deck ALL=(ALL) NOPASSWD: ALL\n')
        sudoers.chmod(0o440)

    def _desktop_entry(self, name, comment, exec_args, icon):
        """Build a .desktop launcher entry invoking install_to_hd.sh.

        Args:
            name: Display name (used for both Name and GenericName).
            comment: Descriptive comment shown in the launcher.
            exec_args: Argument passed to install_to_hd.sh ('all' or
                'system').
            icon: Icon theme name to use.

        Returns:
            The full .desktop file contents, as a string.
        """
        return (
            '[Desktop Entry]\n'
            'Name=%s\n'
            'GenericName=%s\n'
            'Comment=%s\n'
            'Exec=/home/deck/tools/install_to_hd.sh %s\n'
            'Icon=%s\n'
            'Path=/home/deck\n'
            'Terminal=true\n'
            'Type=Application\n'
            'StartupNotify=true\n' % (name, name, comment, exec_args, icon)
        )

    # --------------------------------------------------------- finishing
    def run_sanity_checks(self):
        """Verify the built image has everything it's supposed to.

        Checks for nvidia.ko, the modprobe blacklist conf, and (in
        selfheal mode) the update wrapper, its preserved original, and
        the self-healing machinery files.

        Raises:
            RuntimeError: If any expected artifact is missing from the
                built image.
        """
        self._log('Sanity checks')

        modules_glob = list((self.mnt / 'usr' / 'lib' / 'modules' / (self.kernel_version or '')).glob('updates/dkms/nvidia.ko*')) \
            if self.kernel_version else []
        if not modules_glob:
            self._die('nvidia.ko missing from image')

        conf = self.mnt / 'etc' / 'modprobe.d' / '99-nvidia-patch.conf'
        if not conf.exists() or 'blacklist nouveau' not in conf.read_text():
            self._die('modprobe conf is empty/missing')

        if self.update_mode == 'selfheal':
            wrapper = self.mnt / 'usr' / 'bin' / 'steamos-update'
            if not wrapper.exists() or 'self-healing' not in wrapper.read_text():
                self._die('update wrapper missing')
            if not (self.mnt / 'usr' / 'bin' / 'steamos-update.orig').exists():
                self._die('original steamos-update not preserved')

            lib_dir = self.mnt / 'usr' / 'lib' / 'steamos-nvidia'
            if not (lib_dir / 'repatch.py').exists():
                self._die('repatch.py missing')
            if not (lib_dir / 'driver.json').exists():
                self._die('driver.json missing')

    def sync_and_finalize(self):
        """Flush the mounted partitions and restore the btrfs read-only property."""
        self._log('Syncing filesystems')
        if self.root_fs_type == 'btrfs':
            self._run(['btrfs', 'filesystem', 'sync', str(self.mnt)])
        self._run(['sync', '-f', str(self.mnt)])
        self._run(['sync', '-f', str(self.homemnt)])
        self._run(['sync', '-f', str(self.efimnt)])

        if self.root_fs_type == 'btrfs':
            self._log('Restoring btrfs read-only property')
            self._run(['btrfs', 'property', 'set', str(self.mnt), 'ro', 'true'])

    def _quiet_umount(self, path):
        """Unmount path if (and only if) it's actually mounted.

        Retries a few times before falling back to a lazy unmount (-l).
        A lazy unmount is a LAST RESORT, not a safe equivalent: it
        detaches the mountpoint from the namespace immediately, but the
        kernel finishes flushing and actually releasing the filesystem
        only once nothing references it anymore — which can still be in
        progress when this returns. Detaching the backing loop device
        (or writing out the image file) right after a lazy unmount,
        before that settles, is exactly how a partition ends up with its
        dirty bit still set and fails fsck on first real boot.
        self._had_unclean_unmount is set so cleanup() can wait it out
        before touching the loop device.

        Args:
            path: Path to unmount.
        """
        if not self._is_mountpoint(path):
            return

        for attempt in range(3):
            result = self._run_quiet(['umount', '-R', str(path)])
            if result is not None and result.returncode == 0:
                return
            if attempt < 2:
                time.sleep(1)

        # Still busy after retries — something is holding it open (a
        # lingering daemon left running inside a chroot is the usual
        # culprit; see _kill_chroot_processes). Force the issue, then lazy
        # unmount as an actual last resort.
        self._kill_chroot_processes(path)
        result = self._run_quiet(['umount', '-R', str(path)])
        if result is not None and result.returncode == 0:
            return

        self._warn('%s still busy after retries — falling back to a lazy unmount '
                    '(umount -Rl); this is not a clean detach' % path)
        retry = self._run_quiet(['umount', '-Rl', str(path)])
        if retry is None or retry.returncode != 0:
            self._warn('Could not unmount %s at all (may need manual cleanup)' % path)
            return

        self._had_unclean_unmount = True
        # Give the lazy unmount a real chance to finish settling before
        # anything downstream (losetup -d, closing the image file) assumes
        # the filesystem is actually released.
        for _ in range(10):
            if not self._is_mountpoint(path):
                break
            time.sleep(1)
        else:
            self._warn('%s did not fully release after a lazy unmount — the built '
                        'image may have an unclean filesystem on it' % path)

    def _kill_chroot_processes(self, path):
        """Best-effort SIGKILL every process actually chrooted into path.

        pacman-key --populate (run inside the build chroot) spawns
        gpg-agent/dirmngr, which keep running in the background with
        their cwd/root pinned inside the chroot even after we're done
        with it — holding the mount busy for no reason we still care
        about. Never fatal.

        DELIBERATELY NOT `fuser -km <path>`. self.merged has /dev and
        /sys --rbind-mounted from the HOST's real /dev and /sys — they
        are the exact same underlying inodes, not copies. fuser matches
        processes by open file descriptors against inode identity, so
        `fuser -km` against a path containing those bind-mounts can
        match (and SIGKILL) ANY host process that simply has /dev/null,
        /dev/urandom, or a tty open — which is effectively every process
        on the machine, up to and including things that take the whole
        desktop down with them. An earlier version of this method used
        fuser directly; that was a real, dangerous bug (confirmed: it
        was crashing/rebooting the build host). Instead, only kill
        processes that are actually chrooted INTO this exact path,
        identified via /proc/<pid>/root — a lingering gpg-agent from
        inside the chroot matches; an unrelated host process that merely
        has a device file open under a bind-mounted subtree does not,
        because its /proc/<pid>/root is "/", not this path.

        Args:
            path: Path whose chrooted processes should be killed.
        """
        try:
            target = os.path.realpath(str(path))
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
                os.kill(int(pid_dir.name), signal.SIGKILL)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        time.sleep(0.5)

    def cleanup(self):
        """Tear down every mount and loop device this build set up.

        Always runs (via the build() finally block) regardless of
        whether the build succeeded, so a failed build never leaves
        stray mounts or an attached loop device behind.
        """
        self._had_unclean_unmount = False
        for mountpoint in (self.merged, self.efimnt, self.homemnt, self.mnt):
            if mountpoint is None:
                continue
            # pacman-key --populate (run earlier, inside self.merged) spawns
            # gpg-agent/dirmngr, which are actually chrooted at mountpoint
            # itself (their /proc/<pid>/root == mountpoint) but can hold a
            # SUBMOUNT like mountpoint/dev busy via an open fd (a pty under
            # dev/pts, /dev/urandom, etc.) without being chrooted into that
            # submount specifically. _quiet_umount below only ever kills
            # processes rooted at the exact submount it's unmounting, so a
            # process rooted at mountpoint never matched and this used to
            # fall through to a lazy unmount every time. Kill anything
            # actually chrooted at mountpoint up front, before touching any
            # of its submounts, so the unmounts below don't need to.
            self._kill_chroot_processes(mountpoint)
            for sub in ('dev/pts', 'dev', 'sys', 'proc'):
                self._quiet_umount(mountpoint / sub)
            self._quiet_umount(mountpoint)
        # Unmount the build filesystem (holds upper/ovlwork) only after
        # merged (the overlay mount backed by it) is already gone above.
        if self.buildfs_mnt is not None:
            self._quiet_umount(self.buildfs_mnt)

        if self.loopdev:
            # Flush everything to the backing image file before detaching —
            # a plain `sync` (not just sync -f on specific mountpoints,
            # which by now may already be unmounted) plus a block-device
            # buffer flush, so nothing is left dirty in memory when the
            # loop device goes away.
            if self._had_unclean_unmount:
                self._warn(
                    'At least one mount needed a lazy unmount during cleanup — '
                    'waiting a bit longer before detaching the loop device'
                )
                time.sleep(3)
            self._run_quiet(['sync'])
            self._run_quiet(['blockdev', '--flushbufs', self.loopdev])
            self._run(['losetup', '-d', self.loopdev])

    # ---------------------------------------------------------------- run
    def build(self):
        """Run the full build pipeline end to end.

        Output is grouped into numbered phase banners rather than a flat
        stream of command output, so progress is easy to follow. Cleanup
        always runs, even on failure, before verify_image_integrity() is
        attempted.

        Returns:
            The Path to the built output image.

        Raises:
            RuntimeError: If any build phase fails.
        """
        phases = [
            ('Checking prerequisites', (
                self.check_running_as_root,
                self.check_required_tools,
                self.validate_driver_spec,
            )),
            ('Preparing image', (
                self.resolve_image_path,
                self.prepare_workdir,
                self.copy_image,
                self.loop_mount,
            )),
            ('Mounting partitions', (
                self.discover_partitions,
            )),
            ('Discovering kernel + headers', (
                self.discover_kernel_version,
                self.resolve_headers_url,
            )),
            ('Resolving NVIDIA driver packages', (
                self.resolve_driver_packages,
            )),
            ('Checking glibc compatibility', (
                self.check_glibc_compatibility,
            )),
            ('Building driver in overlay chroot', (
                self.setup_overlay_chroot,
                self.build_driver,
            )),
            ('Installing driver payload', (
                self.compute_payload,
                self.install_payload,
                self.configure_modprobe,
            )),
            ('Configuring update strategy (%s)' % self.update_mode, (
                self.configure_update_strategy,
            )),
            ('Patching kernel cmdline', (
                self.patch_kernel_cmdline,
            )),
            ('Installing one-click USB installer', (
                self.install_one_click_installer,
            )),
            ('Running sanity checks', (
                self.run_sanity_checks,
            )),
            ('Finalizing image', (
                self.sync_and_finalize,
            )),
        ]

        total = len(phases)
        try:
            for index, (name, steps) in enumerate(phases, start=1):
                with self._step('[%d/%d] %s' % (index, total, name)):
                    for step in steps:
                        step()
            print()
            self._log('DONE — %s' % self.out_image)
        finally:
            self.cleanup()

        with self._step('Verifying image integrity'):
            self.verify_image_integrity()

        return self.out_image

    def verify_image_integrity(self):
        """Run a best-effort, read-only fsck pass over the finished image.

        Re-attaches the image and runs `e2fsck -n` (check only, never
        fixes, never writes) against each ext4 partition. This exists to
        catch an unclean unmount (see _quiet_umount) at build time — as
        a clear warning here — instead of discovering it as a fsck
        failure the first time the USB actually boots. Never raises; a
        problem here is reported as a warning, since dd'ing this exact
        image is still the user's call to make with the information.
        """
        result = self._run_quiet(['losetup', '-f', '--show', '-r', '-P', str(self.out_image)])
        loop = result.stdout.strip() if result and result.returncode == 0 and result.stdout else None
        if not loop:
            self._warn('Could not re-attach the built image to verify it (non-fatal, skipping)')
            return

        try:
            found = {}
            for part in sorted(Path('/dev').glob(os.path.basename(loop) + 'p*')):
                blk = self._run_quiet(['blkid', '-p', '-s', 'PART_ENTRY_NAME', '-o', 'value', str(part)])
                name = blk.stdout.strip() if blk and blk.stdout else ''
                if name:
                    found.setdefault(name, part)

            checks = (
                ('root', self.ROOT_PART_NAMES),
                ('home', self.HOME_PART_NAMES),
                ('var', ('var',)),
            )
            all_clean = True
            checked_any = False
            for label, names in checks:
                part = next((found[n] for n in names if n in found), None)
                if part is None:
                    continue
                if self._detect_fs_type(part) != 'ext4':
                    continue  # e2fsck only applies to ext*; btrfs has its own checks
                checked_any = True
                check = self._run_quiet(['e2fsck', '-n', '-f', str(part)])
                # e2fsck exit codes: 0 = clean. Anything else (with -n, which
                # never fixes anything) means it found something wrong.
                if check is None or check.returncode != 0:
                    all_clean = False
                    self._warn(
                        '%s partition (%s) failed a read-only fsck check (exit %s) — '
                        'this image likely has the same unclean-unmount problem that '
                        'causes "Failed to start File System Check" at boot. Do not '
                        'flash it; rebuild instead.'
                        % (label, part, check.returncode if check else 'unknown')
                    )

            if checked_any and all_clean:
                self._log('Filesystem integrity check passed (root/home/var all clean)')
            elif not checked_any:
                self._log('No ext4 partitions to verify (skipped)')
        finally:
            self._run_quiet(['losetup', '-d', loop])
