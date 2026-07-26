# SteamOS-Utils

A set of command-line tools for devices running SteamOS, run through a single
entry point, `steam_os_utils.py`. Each tool is implemented as its own class
or module and can also be imported and used directly from Python.

Everything here that touches OS/root-level state (mounts, chroots, pacman,
dkms, disk writes) shells out to real system tools. There is no dry-run or
preview mode — every invocation acts for real, including the disk-wiping
one-click USB installer flow described below.

## Requirements

- Python 3 (no third-party packages required — everything uses the standard
  library).
- Must be run **as root** for anything that touches the live system or a
  mounted image (`-acpi`, `-build_nvidia_usb`).
- The NVIDIA USB image builder additionally requires these host tools to be
  on `PATH`: `losetup`, `blkid`, `btrfs`, `rsync`, `curl`, `depmod`, `sed`,
  `awk`, `tar`, `zstd`, `pacman`, `readelf`, `chroot`, `mount`, `umount`,
  `udevadm`, `findmnt`, `mountpoint`, `mkfs.ext4`, `truncate`, `blockdev`,
  `setpriv`.

## Layout

```
steam_os_utils.py                    Entry point / CLI
acpi_enabler.py                      AcpiEnabler — DKMS ACPI call enabler
legion_go2_brightness_slider.py      Legion Go 2 brightness slider fix (stub)
package_downloader.py                PackageDownloader — shared, hardened
                                      download helper (atomic, cache-aware)
dkms_supported_versions.py           Kernel version / package filename helpers
shell_utils.py                       Shared run_command() helper
file_downloader.py                   Legacy downloader, unused, kept for
                                      reference
nvidia_usb_image_builder/            NVIDIA USB installer image builder
    __init__.py                      Re-exports NvidiaUsbImageBuilder
    nvidia_usb_image_builder.py      NvidiaUsbImageBuilder — the build itself
    update_wrapper_script_builder.py Generates the on-device self-heal
                                      update wrapper(s)
    repatch_script.py                Standalone on-device script
                                      (installed as repatch.py) that rebuilds
                                      the driver after an OS update
    install_to_hd.sh                 Standalone one-click installer script
                                      shipped inside the built image
recovery/                            Manual disaster-recovery scripts for
                                      use from rescue/live media
```

## Usage

Run the entry point with `-h` at any time to see the full, current flag
list:

```bash
sudo python3 steam_os_utils.py -h
```

### Enable ACPI calls (DKMS)

Downloads the linux-neptune kernel modules + headers packages matching the
*currently running* kernel (resolved from the live system's own
`/etc/pacman.conf` and mirrorlist) and installs them via pacman, bracketed
by disabling/re-enabling `steamos-readonly`.

```bash
sudo python3 steam_os_utils.py -acpi
```

Equivalently from Python:

```python
from acpi_enabler import AcpiEnabler

AcpiEnabler().enable()
```

### Legion Go 2 brightness slider fix

Enables (or removes) the Legion Go 2 brightness slider and color-correction
fix. These are currently stub implementations pending the real fix logic.

```bash
sudo python3 steam_os_utils.py -lego2brightness
sudo python3 steam_os_utils.py -removelego2brightness
```

### Build a one-click NVIDIA USB installer image

Turns a clean SteamOS OOBE repair image into a self-healing USB installer
with the NVIDIA-open (RTX 20xx+) driver baked in: mounts the image, builds
the driver in an isolated overlay chroot against the exact pinned Arch
package set, patches the kernel cmdline, installs the self-heal update
machinery (so a future OS update rebuilds the driver in the newly staged
slot automatically), and optionally adds a one-click "install to internal
disk" desktop icon.

```bash
# Simplest form: if exactly one *.img sits next to
# nvidia_usb_image_builder/nvidia_usb_image_builder.py, it's
# auto-detected. Builds with the latest pinned driver, in self-heal
# update mode, with the one-click installer added. Output defaults to
# <input-name>-nvidia.img alongside the input.
sudo python3 steam_os_utils.py -build_nvidia_usb

# Same, but pass the image explicitly (positional argument) and pick
# where the output goes.
sudo python3 steam_os_utils.py -build_nvidia_usb \
    steamdeck-oobe-repair-3.8.img \
    -nvidia_output steamdeck-oobe-repair-3.8-nvidia.img

# Pin an exact driver version instead of 'latest'.
sudo python3 steam_os_utils.py -build_nvidia_usb steamdeck-oobe-repair-3.8.img \
    -nvidia_driver 580.105.08

# Build a plain patched OS image with no one-click installer icon.
sudo python3 steam_os_utils.py -build_nvidia_usb steamdeck-oobe-repair-3.8.img \
    -nvidia_no_installer

# Shrink the image by ~350 MB by dropping CUDA/OpenCL/NVVM/OptiX libraries.
sudo python3 steam_os_utils.py -build_nvidia_usb steamdeck-oobe-repair-3.8.img \
    -nvidia_trim_cuda

# Hold OS updates entirely instead of self-healing them (masks the
# updater services and stubs the update CLIs).
sudo python3 steam_os_utils.py -build_nvidia_usb steamdeck-oobe-repair-3.8.img \
    -nvidia_update_mode hold

# Build a fully stock update image (an OS update will remove the driver!).
sudo python3 steam_os_utils.py -build_nvidia_usb steamdeck-oobe-repair-3.8.img \
    -nvidia_update_mode stock

# Disable pacman signature checks in the build chroot (useful if the
# image's frozen keyring predates current Arch packager keys).
sudo python3 steam_os_utils.py -build_nvidia_usb steamdeck-oobe-repair-3.8.img \
    -nvidia_skip_sigcheck

# Use a specific, reusable build working directory (downloaded packages
# are cached there between runs) instead of the default alongside the
# output image.
sudo python3 steam_os_utils.py -build_nvidia_usb steamdeck-oobe-repair-3.8.img \
    -nvidia_workdir /var/tmp/nvidia-build

# Print every underlying shell command as it runs (useful for debugging
# a failed build).
sudo python3 steam_os_utils.py -build_nvidia_usb steamdeck-oobe-repair-3.8.img \
    -nvidia_verbose

# Combine several options at once.
sudo python3 steam_os_utils.py -build_nvidia_usb steamdeck-oobe-repair-3.8.img \
    -nvidia_driver 580 \
    -nvidia_update_mode selfheal \
    -nvidia_trim_cuda \
    -nvidia_workdir /var/tmp/nvidia-build \
    -nvidia_verbose
```

Equivalently from Python:

```python
from nvidia_usb_image_builder import NvidiaUsbImageBuilder

builder = NvidiaUsbImageBuilder(
    image_path='steamdeck-oobe-repair-3.8.img',
    output_path='steamdeck-oobe-repair-3.8-nvidia.img',
    driver_spec='latest',       # 'latest' or an Arch version prefix, e.g. '580.105.08'
    update_mode='selfheal',     # 'selfheal' | 'hold' | 'stock'
    add_installer=True,         # add the one-click USB installer
    trim_cuda=False,            # drop CUDA/OpenCL/NVVM/OptiX libraries
    skip_sigcheck=False,        # disable pacman signature checks
    workdir=None,                # default: alongside the output image
    verbose=False,
)
builder.build()
```

#### `-build_nvidia_usb` flag reference

| Flag | Default | Description |
| --- | --- | --- |
| `nvidia_image` (positional) | auto-detected | Path to the clean SteamOS OOBE repair `.img` to patch. |
| `-nvidia_output` / `--nvidia_output_path` | `<input>-nvidia.img` | Path for the built installer image. |
| `-nvidia_driver` / `--nvidia_driver_spec` | `latest` | `'latest'` or an Arch version prefix, e.g. `580` / `580.105.08`. |
| `-nvidia_update_mode` / `--nvidia_update_mode` | `selfheal` | `selfheal`, `hold`, or `stock` — how OS updates interact with the driver. |
| `-nvidia_no_installer` / `--nvidia_no_installer` | off | Skip adding the one-click USB installer. |
| `-nvidia_trim_cuda` / `--nvidia_trim_cuda` | off | Drop CUDA/OpenCL/NVVM/OptiX libraries (~350 MB smaller). |
| `-nvidia_skip_sigcheck` / `--nvidia_skip_sigcheck` | off | Disable pacman signature checks in the build chroot. |
| `-nvidia_workdir` / `--nvidia_workdir` | alongside the output image | Build working directory (cached between runs). |
| `-nvidia_verbose` / `--nvidia_verbose` | off | Print each underlying shell command as it runs. |

### Once the image is booted from USB: the one-click installer

If the image was built with the installer (the default), booting it and
using the "install to disk" desktop icon runs `install_to_hd.sh`, which
picks an internal disk and clones the running USB system onto it:

```bash
# Full install: wipes the target disk (default).
sudo /home/deck/tools/install_to_hd.sh all

# Upgrade: reimages the OS partitions only, keeps games & data.
sudo /home/deck/tools/install_to_hd.sh system
```

### Update modes explained

- **`selfheal`** (default): installs `repatch.py` and `driver.json` onto the
  device, and wraps `steamos-update` / `steamos-update-os` /
  `steamos-atomupd-client` so that after a real OS update stages a new slot,
  the driver is automatically rebuilt into it before the update is allowed
  to complete. If the repatch fails, the update is cancelled at the
  bootloader level so the current, still-working slot keeps booting.
- **`hold`**: masks the update services and stubs the update CLIs, so OS
  updates are blocked outright rather than healed.
- **`stock`**: no self-heal machinery is installed at all — a future OS
  update will remove the NVIDIA driver.

### Manual disaster recovery

If self-heal ever fails to run automatically (or you need to repatch a slot
from rescue/live media rather than from an already-booted, patched SteamOS
install), see the scripts under `recovery/`, e.g.:

```bash
sudo DRIVER_CONF=/tmp/driver.conf \
     ROOTDEV=/dev/nvme1n1p4 EFIDEV=/dev/nvme1n1p2 \
     bash recovery/repatch-recovery.sh A
```

## License

MIT License. Copyright (c) 2025 InnoVision Games.
