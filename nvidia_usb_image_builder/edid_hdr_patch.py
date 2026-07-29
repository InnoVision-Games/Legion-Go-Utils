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
# file: edid_hdr_patch.py
#
# steamos-nvidia EDID HDR safety net -- runs on the TARGET machine (not the
# build host: NvidiaUsbImageBuilder cannot know what monitor a given
# installed system will actually be driven by, since the machine that
# builds the USB image and the machine it gets installed on are routinely
# different hardware). Installed at
# /usr/lib/steamos-nvidia/edid_hdr_patch.py by
# NvidiaUsbImageBuilder.configure_edid_hdr_safety(), run at every boot by
# the steamos-nvidia-edid-patch.service oneshot unit it also installs.
#
# WHY THIS EXISTS
# ----------------
# There is a confirmed, currently-open NVIDIA driver bug (tracked on
# NVIDIA's developer forum: "Display modes above 2560x1440p@120hz with HDR
# enabled cause flickering/corruption within gamescope-session") where
# toggling HDR on a NVIDIA GPU, on a mode at or above that
# resolution+refresh combination, corrupts the display. It reproduces on
# current hardware and is not something this project can fix upstream.
# The practical workaround people use today is dropping resolution
# entirely, which throws away native 4K for no good reason -- the bug is
# specifically about resolution+refresh+HDR TOGETHER, not resolution
# alone. A 4K@60Hz HDR mode is fine; a 4K@120Hz HDR mode is not.
#
# This script keeps native 4K by editing what the MONITOR ITSELF claims to
# support: it reads the real EDID from the connected display, removes just
# the specific timing entries that fall in the unsafe zone, and leaves
# everything else (including lower-refresh 4K HDR modes) untouched. With
# those entries gone, Steam's display settings / steamos-manager never
# offers the broken combination in the first place, because the OS
# genuinely no longer believes the monitor supports it.
#
# SCOPE / KNOWN LIMITATIONS (please read before relying on this)
# ----------------------------------------------------------------
# - Only Detailed Timing Descriptors (DTDs) are patched -- both the extra
#   DTD slots in the base EDID block and any DTDs listed in a CTA-861
#   extension block. DTDs are self-describing (an explicit pixel clock +
#   blanking values we can compute an exact refresh rate from), so this
#   is reliable. This script deliberately does NOT attempt to strip
#   individual VIC entries from CTA Short Video Descriptor (SVD) lists --
#   correctly mapping VIC numbers to resolution/refresh requires a large,
#   easy-to-get-subtly-wrong lookup table, and a wrong entry is worse than
#   no entry. In practice, PC/TV monitors that expose a high-bandwidth
#   HDR mode via VIC also list it as a DTD, so this still covers the
#   common real-world case, but it is not a full guarantee for every
#   monitor.
# - The HDMI Forum bandwidth cap (see _cap_hf_vsdb_bandwidth below) only
#   exists on HDMI connections. DisplayPort bandwidth is negotiated over
#   the DPCD AUX channel, not carried in EDID at all, so this script
#   cannot influence it. If your unsafe combination is happening over DP,
#   this mitigation will still remove the matching DTDs (which does
#   help), but it cannot add the same second layer of defense HDMI gets.
# - Never touches descriptor slot 1 (the monitor's preferred/native
#   timing) even if it happens to land in the unsafe zone, since some
#   sinks expect it to always be present and valid. In practice this slot
#   is essentially never the vendor's preferred timing for a monitor.
# - Recomputed EDID checksums are always valid, so a bug in this script
#   fails safe: a GPU/driver presented with a corrupt-checksum EDID
#   simply falls back to a default timing rather than doing something
#   worse. It does NOT fail silently, though -- this script also verifies
#   its own checksum math before writing anything to disk.
#
# Idempotent per monitor: a hash of each connector's RAW (pre-patch) EDID
# is kept in STATE_PATH. Unless a new/changed monitor is plugged in, every
# subsequent boot after the first is a fast no-op -- no rewrite, no grub
# regen, no reboot.

"""
steamos-nvidia EDID HDR safety net -- neutralizes the specific
resolution+refresh+HDR combination known to corrupt the display on NVIDIA
GPUs under gamescope-session, by editing the connected monitor's EDID
rather than capping resolution globally. Run as root, on every boot, via
steamos-nvidia-edid-patch.service. Idempotent; logs to stdout.
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

DRM_CLASS = Path('/sys/class/drm')
FIRMWARE_DIR = Path('/lib/firmware/edid')
STATE_PATH = Path('/var/lib/steamos-nvidia/edid-patch-state.json')
DEFAULT_GRUB = Path('/etc/default/grub')
GRUB_CFG = Path('/esp/EFI/steamos/grub.cfg')

# Optional on-device override, so the unsafe threshold can be tuned (e.g.
# if a future NVIDIA driver moves it) without rebuilding the image.
CONFIG_PATH = Path('/etc/steamos-nvidia/edid-safety.json')
DEFAULT_UNSAFE_MIN_WIDTH = 2560
DEFAULT_UNSAFE_MIN_HEIGHT = 1440
DEFAULT_UNSAFE_MIN_REFRESH_HZ = 120.0

# HDR Static Metadata Data Block: CTA-861 extended tag code 6, carried
# inside a Data Block with top-level tag code 7 ("use extended tag").
_HDR_STATIC_METADATA_EXT_TAG = 6

# HDMI Forum Vendor-Specific Data Block: top-level tag code 3, 24-bit IEEE
# OUI c4-5d-d8 (little-endian in the data block: d8 5d c4).
_HF_VSDB_OUI = b'\xd8\x5d\xc4'

DUMMY_DESCRIPTOR = bytes([0, 0, 0, 0x10] + [0] * 14)  # VESA "unused" descriptor, 18 bytes


def log(msg):
    print('[edid-hdr-patch] %s' % msg, flush=True)


def warn(msg):
    print('[edid-hdr-patch] WARNING: %s' % msg, file=sys.stderr, flush=True)


def load_thresholds():
    """Load the unsafe-zone thresholds, allowing an on-device override.

    Returns:
        (min_width, min_height, min_refresh_hz) tuple.
    """
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            return (
                int(cfg.get('unsafe_min_width', DEFAULT_UNSAFE_MIN_WIDTH)),
                int(cfg.get('unsafe_min_height', DEFAULT_UNSAFE_MIN_HEIGHT)),
                float(cfg.get('unsafe_min_refresh_hz', DEFAULT_UNSAFE_MIN_REFRESH_HZ)),
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            warn('ignoring unreadable %s: %s' % (CONFIG_PATH, exc))
    return (DEFAULT_UNSAFE_MIN_WIDTH, DEFAULT_UNSAFE_MIN_HEIGHT, DEFAULT_UNSAFE_MIN_REFRESH_HZ)


# --------------------------------------------------------------- discovery
def discover_connectors():
    """Find every connected display with a readable EDID.

    Returns:
        List of (kernel_connector_name, raw_edid_bytes) tuples, e.g.
        [('DP-1', b'...'), ('HDMI-A-1', b'...')]. kernel_connector_name is
        already in the form drm.edid_firmware= expects (no "cardN-"
        prefix).
    """
    found = []
    if not DRM_CLASS.is_dir():
        return found
    for entry in sorted(DRM_CLASS.iterdir()):
        status_file = entry / 'status'
        edid_file = entry / 'edid'
        if not (status_file.exists() and edid_file.exists()):
            continue
        try:
            status = status_file.read_text().strip()
        except OSError:
            continue
        if status != 'connected':
            continue
        try:
            raw = edid_file.read_bytes()
        except OSError:
            continue
        if len(raw) < 128 or len(raw) % 128 != 0:
            continue  # no EDID present, or a short/garbage read
        connector = re.sub(r'^card\d+-', '', entry.name)
        found.append((connector, raw))
    return found


# ------------------------------------------------------------- EDID math
def _checksum(block128):
    """Compute the VESA checksum byte for a 128-byte EDID block."""
    return (256 - sum(block128[:127]) % 256) % 256


def _fix_checksum(block128):
    block = bytearray(block128)
    block[127] = _checksum(bytes(block))
    return bytes(block)


def _parse_dtd(desc18):
    """Parse an 18-byte descriptor as a Detailed Timing Descriptor.

    Returns:
        dict with pixel_clock_khz/h_active/v_active/refresh_hz, or None
        if this descriptor isn't a DTD (pixel clock == 0, i.e. it's a
        Display Descriptor -- monitor name, range limits, dummy, etc).
    """
    pixel_clock_raw = desc18[0] | (desc18[1] << 8)
    if pixel_clock_raw == 0:
        return None
    pixel_clock_khz = pixel_clock_raw * 10
    h_active = desc18[2] | ((desc18[4] & 0xF0) << 4)
    h_blank = desc18[3] | ((desc18[4] & 0x0F) << 8)
    v_active = desc18[5] | ((desc18[7] & 0xF0) << 4)
    v_blank = desc18[6] | ((desc18[7] & 0x0F) << 8)
    h_total = h_active + h_blank
    v_total = v_active + v_blank
    if h_total == 0 or v_total == 0:
        return None
    refresh_hz = (pixel_clock_khz * 1000.0) / (h_total * v_total)
    return {
        'pixel_clock_khz': pixel_clock_khz,
        'h_active': h_active,
        'v_active': v_active,
        'refresh_hz': refresh_hz,
    }


def _is_unsafe(dtd, thresholds):
    min_w, min_h, min_hz = thresholds
    return (dtd['h_active'] >= min_w and dtd['v_active'] >= min_h
            and dtd['refresh_hz'] >= min_hz - 0.5)  # small tolerance for rounding


def _patch_descriptor_slots(block128, offsets, thresholds, protect_first=False):
    """Neutralize unsafe DTDs at the given 18-byte-descriptor offsets.

    Args:
        block128: The 128-byte block (base EDID or a CTA extension)
            containing these descriptor slots.
        offsets: Byte offsets of each 18-byte descriptor within the
            block.
        thresholds: (min_width, min_height, min_refresh_hz).
        protect_first: If True, never touch offsets[0] (used for the
            base block's slot 1 / preferred timing -- see module
            docstring).

    Returns:
        (patched_block, changed) tuple.
    """
    block = bytearray(block128)
    changed = False
    for index, offset in enumerate(offsets):
        if protect_first and index == 0:
            continue
        desc = bytes(block[offset:offset + 18])
        dtd = _parse_dtd(desc)
        if dtd is None:
            continue
        if _is_unsafe(dtd, thresholds):
            log('  neutralizing %dx%d@%.1fHz timing (unsafe zone)'
                % (dtd['h_active'], dtd['v_active'], dtd['refresh_hz']))
            block[offset:offset + 18] = DUMMY_DESCRIPTOR
            changed = True
    return bytes(block), changed


def _find_hf_vsdb_rate_byte_offset(cta_block, dtd_start):
    """Find the byte offset of Max_TMDS_Character_Rate inside an HF-VSDB.

    Walks the CTA Data Block Collection (bytes 4..dtd_start) looking for
    the HDMI Forum Vendor-Specific Data Block (tag 3, OUI c4-5d-d8). Its
    payload layout (after the 3-byte OUI) is fixed by the HDMI 2.1 spec:
    byte 0 = version, byte 1 = Max_TMDS_Character_Rate (in units of 5MHz;
    0 means "no HDMI 2.1 fixed rate link, use HDMI 2.0 rules instead").

    Returns:
        Absolute byte offset of the rate byte within cta_block, or None
        if no HF-VSDB is present.
    """
    pos = 4
    while pos < dtd_start:
        header = cta_block[pos]
        tag = (header & 0xE0) >> 5
        length = header & 0x1F
        payload_start = pos + 1
        payload_end = payload_start + length
        if payload_end > dtd_start:
            break  # malformed collection; stop rather than read garbage
        if tag == 3 and length >= 5 and cta_block[payload_start:payload_start + 3] == _HF_VSDB_OUI:
            rate_offset = payload_start + 4  # OUI(3) + version(1) -> rate byte
            if rate_offset < payload_end:
                return rate_offset
        pos = payload_end
    return None


def _has_hdr_static_metadata(cta_block, dtd_start):
    """Check the CTA Data Block Collection for an HDR Static Metadata block."""
    pos = 4
    while pos < dtd_start:
        header = cta_block[pos]
        tag = (header & 0xE0) >> 5
        length = header & 0x1F
        payload_start = pos + 1
        payload_end = payload_start + length
        if payload_end > dtd_start:
            break
        if tag == 7 and length >= 1 and cta_block[payload_start] == _HDR_STATIC_METADATA_EXT_TAG:
            return True
        pos = payload_end
    return False


def _cap_hf_vsdb_bandwidth(cta_block, dtd_start, thresholds):
    """Cap Max_TMDS_Character_Rate so the unsafe mode can't be negotiated at all.

    Second, independent line of defense on top of DTD stripping (HDMI
    only -- see module docstring). 600MHz (rate byte 120, since the unit
    is 5MHz) comfortably covers 4K@60 4:4:4 HDR while sitting below what
    4K@120 4:4:4 HDR needs, without touching anything else in the block.
    Only lowers the value -- never raises it above what the monitor
    itself already advertised.

    Returns:
        (patched_block, changed) tuple.
    """
    min_w, min_h, min_hz = thresholds
    if min_w > 3840 or min_h > 2160:
        return cta_block, False  # threshold above 4K -- nothing to cap for the common case
    rate_offset = _find_hf_vsdb_rate_byte_offset(cta_block, dtd_start)
    if rate_offset is None:
        return cta_block, False
    block = bytearray(cta_block)
    current = block[rate_offset]
    safe_cap = 120  # 120 * 5MHz = 600MHz
    if current == 0 or current <= safe_cap:
        return cta_block, False  # already at/below cap, or "no fixed-rate link" already
    log('  capping HDMI Forum Max_TMDS_Character_Rate: %dMHz -> %dMHz'
        % (current * 5, safe_cap * 5))
    block[rate_offset] = safe_cap
    return bytes(block), True


# ------------------------------------------------------------ full patch
def patch_edid(raw, thresholds):
    """Patch one connector's raw EDID.

    Returns:
        (patched_bytes, changed, hdr_capable) tuple. patched_bytes ==
        raw and changed == False whenever hdr_capable is False (nothing
        to mitigate on a non-HDR display) or nothing in the unsafe zone
        was found.
    """
    base = bytearray(raw[:128])
    ext_count = base[126]
    extensions = [bytearray(raw[128 * (i + 1):128 * (i + 2)]) for i in range(ext_count)]

    hdr_capable = False
    for ext in extensions:
        if len(ext) == 128 and ext[0] == 0x02:  # CTA-861 extension tag
            dtd_start = ext[2]
            if dtd_start and _has_hdr_static_metadata(bytes(ext), dtd_start):
                hdr_capable = True
                break
    if not hdr_capable:
        return raw, False, False

    changed = False

    base_offsets = [54, 72, 90, 108]
    new_base, base_changed = _patch_descriptor_slots(
        bytes(base), base_offsets, thresholds, protect_first=True)
    if base_changed:
        new_base = _fix_checksum(new_base)
        base = bytearray(new_base)
        changed = True

    new_extensions = []
    for ext in extensions:
        if len(ext) != 128 or ext[0] != 0x02:
            new_extensions.append(bytes(ext))
            continue
        dtd_start = ext[2]
        ext_bytes = bytes(ext)
        ext_changed = False
        if dtd_start:
            dtd_offsets = list(range(dtd_start, 127, 18))
            ext_bytes, dtd_ch = _patch_descriptor_slots(ext_bytes, dtd_offsets, thresholds)
            ext_changed = ext_changed or dtd_ch
            ext_bytes, cap_ch = _cap_hf_vsdb_bandwidth(ext_bytes, dtd_start, thresholds)
            ext_changed = ext_changed or cap_ch
        if ext_changed:
            ext_bytes = _fix_checksum(ext_bytes)
            changed = True
        new_extensions.append(ext_bytes)

    patched = bytes(base) + b''.join(new_extensions)

    # Fail-safe self-check: never write out something we wouldn't accept
    # back in ourselves. _checksum() expects a full 128-byte block and
    # reads only bytes [0:127] internally, so pass the block itself (not
    # a 127-byte slice) and compare against the stored checksum byte.
    if patched[127] != _checksum(patched[:128]):
        warn('base block checksum verification failed after patch -- discarding patch')
        return raw, False, hdr_capable
    for i in range(len(new_extensions)):
        off = 128 * (i + 1)
        block = patched[off:off + 128]
        if len(block) == 128 and block[127] != _checksum(block):
            warn('extension block %d checksum verification failed after patch -- discarding patch' % i)
            return raw, False, hdr_capable

    return patched, changed, hdr_capable


# ------------------------------------------------------------------ state
def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (ValueError, json.JSONDecodeError):
            pass
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + '\n')


def edid_hash(raw):
    return hashlib.sha256(raw).hexdigest()


# ------------------------------------------------------------- grub/boot
def _current_cmdline_extra(grub_text):
    match = re.search(r'drm\.edid_firmware=(\S+)', grub_text)
    return match.group(1) if match else None


def apply_cmdline(fragment):
    """Idempotently set drm.edid_firmware=<fragment> in GRUB_CMDLINE_LINUX_DEFAULT.

    Args:
        fragment: The full drm.edid_firmware= value (may cover several
            connectors, comma-separated) or None to remove any existing
            override.

    Returns:
        True if /etc/default/grub was actually changed (and therefore
        grub.cfg needs regenerating), False if it already matched.
    """
    if not DEFAULT_GRUB.exists():
        warn('%s not found -- cannot apply EDID override to the kernel cmdline' % DEFAULT_GRUB)
        return False
    text = DEFAULT_GRUB.read_text()
    existing = _current_cmdline_extra(text)
    desired = fragment
    if existing == desired:
        return False

    # Strip any previous drm.edid_firmware= token (monitor may have
    # changed since last boot) before adding the current one.
    text = re.sub(r'\s*drm\.edid_firmware=\S+', '', text)
    if desired:
        text = re.sub(
            r'^(GRUB_CMDLINE_LINUX_DEFAULT="[^"]*)"',
            lambda m: m.group(1) + ' drm.edid_firmware=' + desired + '"',
            text,
            flags=re.M,
        )
    DEFAULT_GRUB.write_text(text)
    return True


def regenerate_grub_and_reboot():
    log('Regenerating grub config')
    result = subprocess.run(['update-grub'], capture_output=True, text=True)
    if result.returncode != 0:
        warn('update-grub failed: %s' % (result.stderr or result.stdout))
        return
    if GRUB_CFG.exists() and 'drm.edid_firmware=' not in GRUB_CFG.read_text():
        warn('regenerated grub.cfg is missing the EDID override -- not rebooting')
        return
    log('Rebooting once to apply the new display timings')
    subprocess.run(['systemctl', 'reboot'], check=False)


# ---------------------------------------------------------------- main
def main():
    thresholds = load_thresholds()
    connectors = discover_connectors()
    if not connectors:
        log('No connected displays with a readable EDID -- nothing to do')
        return 0

    state = load_state()
    fragments = []
    any_new_write = False

    for connector, raw in connectors:
        digest = edid_hash(raw)
        prior = state.get(connector)
        if prior and prior.get('source_hash') == digest:
            log('%s: unchanged since last boot, skipping' % connector)
            if prior.get('fragment'):
                fragments.append(prior['fragment'])
            continue

        patched, changed, hdr_capable = patch_edid(raw, thresholds)
        if not hdr_capable:
            log('%s: not HDR-capable, nothing to mitigate' % connector)
            state[connector] = {'source_hash': digest, 'fragment': None}
            continue
        if not changed:
            log('%s: HDR-capable but no unsafe timings found, leaving as-is' % connector)
            state[connector] = {'source_hash': digest, 'fragment': None}
            continue

        FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', connector)
        firmware_path = FIRMWARE_DIR / ('%s-patched.bin' % safe_name)
        firmware_path.write_bytes(patched)
        fragment = '%s:edid/%s-patched.bin' % (connector, safe_name)
        log('%s: wrote patched EDID -> %s' % (connector, firmware_path))
        fragments.append(fragment)
        state[connector] = {'source_hash': digest, 'fragment': fragment}
        any_new_write = True

    save_state(state)

    combined = ','.join(fragments) if fragments else None
    cmdline_changed = apply_cmdline(combined)

    if cmdline_changed:
        regenerate_grub_and_reboot()
    elif any_new_write:
        # Fragment set didn't change (e.g. same connector re-patched to
        # the same result) but we still wrote new firmware bytes this
        # boot -- nothing further required, they'll be picked up as-is.
        log('Patched EDID(s) written; kernel cmdline already up to date')
    else:
        log('Nothing to do')

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- last-resort unattended guard
        warn(str(exc))
        sys.exit(1)
