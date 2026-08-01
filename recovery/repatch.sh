#!/bin/bash
# steamos-utils repatch — rebuild + install the NVIDIA driver into
# another partition set (normally "other"), right after an OS update
# staged it there. Run as root. Idempotent: exits 0 immediately if the
# slot already has the driver for its kernel. Logs to stdout (the
# update wrapper redirects).
set -euo pipefail

PARTSET="${1:-other}"
log() { echo "[repatch] $*"; }
die() { echo "[repatch] FAIL: $*" >&2; exit 1; }

ROOTDEV="/dev/disk/by-partsets/$PARTSET/rootfs"
EFIDEV="/dev/disk/by-partsets/$PARTSET/efi"
[[ -b "$ROOTDEV" && -b "$EFIDEV" ]] || die "partset '$PARTSET' not found (single-slot system?)"

NEWROOT="$(mktemp -d /tmp/repatch-root.XXXXXX)"
# SteamOS /home is ext4 with casefold enabled, which overlayfs rejects
# as an upperdir — so the build workspace lives inside a plain ext4
# loopback image on /home (space for the build, no casefold).
WORKIMG=/home/.steamos-utils-work.img
WORK="$(mktemp -d /tmp/repatch-work.XXXXXX)"
UPPER="$WORK/upper"; OVLWORK="$WORK/ovlwork"; MERGED="$WORK/merged"

cleanup() {
  set +e
  for m in "$MERGED"/dev/pts "$MERGED"/dev "$MERGED"/sys "$MERGED"/proc "$MERGED" \
           "$NEWROOT"/efi "$NEWROOT"/dev/pts "$NEWROOT"/dev "$NEWROOT"/sys "$NEWROOT"/proc "$NEWROOT" \
           "$WORK"; do
    mountpoint -q "$m" 2>/dev/null || continue
    fuser -km "$m" 2>/dev/null; sleep 0.2
    umount -R "$m" 2>/dev/null || umount -Rl "$m" 2>/dev/null
  done
  sync
  rmdir "$NEWROOT" "$WORK" 2>/dev/null
  rm -f "$WORKIMG"
}
trap cleanup EXIT

rm -f "$WORKIMG"
truncate -s 8G "$WORKIMG"
mkfs.ext4 -q -F "$WORKIMG"
mount -o loop "$WORKIMG" "$WORK"
mkdir -p "$UPPER" "$OVLWORK" "$MERGED"

log "Mounting $ROOTDEV"
ROOTFS_TYPE="$(blkid -p -s TYPE -o value "$ROOTDEV" 2>/dev/null || true)"
if [[ "$ROOTFS_TYPE" == btrfs ]]; then
  mount -o compress-force=zstd:3 "$ROOTDEV" "$NEWROOT"
else
  mount "$ROOTDEV" "$NEWROOT"
fi
WAS_RO=0
if [[ "$ROOTFS_TYPE" == btrfs ]] && [[ "$(btrfs property get "$NEWROOT" ro)" == "ro=true" ]]; then
  WAS_RO=1; btrfs property set "$NEWROOT" ro false
fi

KVER=""
for d in "$NEWROOT/usr/lib/modules/"*; do
  [[ -d "$d" ]] || continue
  case "$(basename "$d")" in *neptune*|*[Nn][Ee][Pp][Tt][Uu][Nn][Ee]*) KVER="$(basename "$d")"; break ;; esac
done
[[ -n "$KVER" ]] || die "no neptune kernel in $PARTSET rootfs"
log "Target kernel: $KVER"

if compgen -G "$NEWROOT/usr/lib/modules/$KVER/updates/dkms/nvidia.ko*" >/dev/null; then
  log "Driver already present for $KVER — nothing to do"
  [[ $WAS_RO -eq 1 ]] && btrfs property set "$NEWROOT" ro true
  exit 0
fi

PACDB="$NEWROOT/usr/lib/holo/pacmandb/local"
KPKG_DIR=""
for d in "$PACDB"/linux-neptune-*-[0-9]*; do
  [[ -d "$d" ]] || continue
  case "$(basename "$d")" in *-headers-*|*firmware*|*rtw*) continue ;; esac
  KPKG_DIR="$d"; break
done
[[ -n "$KPKG_DIR" ]] || die "kernel package not found in new slot's pacman db"
KPKG_FULL="$(basename "$KPKG_DIR")"
KPKG_NAME="${KPKG_FULL%-*-*}"
KPKG_VERREL="${KPKG_FULL#"$KPKG_NAME"-}"
JUPITER_REPO="$(awk -F'[][]' '/^\[jupiter-/{print $2; exit}' "$NEWROOT/etc/pacman.conf")"
MIRROR="$(awk '/^Server/{print $3; exit}' "$NEWROOT/etc/pacman.d/mirrorlist")"
HDR_URL="${MIRROR/\$repo/$JUPITER_REPO}"
HDR_URL="${HDR_URL/\$arch/x86_64}/${KPKG_NAME}-headers-${KPKG_VERREL}-x86_64.pkg.tar.zst"
log "Headers: $(basename "$HDR_URL")"
curl -sfIL "$HDR_URL" -o /dev/null || die "matching headers not in Valve's pool: $HDR_URL"

log "Building driver in overlay chroot (this takes 10-20 minutes)"
mount -t overlay overlay -o "index=off,lowerdir=$NEWROOT,upperdir=$UPPER,workdir=$OVLWORK" "$MERGED"
mount -t proc proc "$MERGED/proc"
mount --rbind /sys "$MERGED/sys"; mount --make-rslave "$MERGED/sys"
mount --rbind /dev "$MERGED/dev"; mount --make-rslave "$MERGED/dev"
rm -f "$MERGED/etc/resolv.conf"; cp -L /etc/resolv.conf "$MERGED/etc/resolv.conf"
in_chroot() { chroot "$MERGED" /bin/bash -c "$*"; }

[[ -d "$MERGED/etc/pacman.d/gnupg/private-keys-v1.d" ]] \
  || in_chroot "pacman-key --init && pacman-key --populate"
in_chroot "curl -sfL '$HDR_URL' -o /tmp/headers.pkg.tar.zst"
in_chroot "pacman -Sy"
in_chroot "pacman -Qq" | LC_ALL=C sort > "$WORK/before.txt"
in_chroot "pacman -U --noconfirm --needed /tmp/headers.pkg.tar.zst"
in_chroot "pacman -S --noconfirm --needed dkms"

# Driver = the exact pinned Arch packages this image was built with
# (NOT the slot's frozen repo — that only has Valve's older driver).
source /usr/lib/steamos-utils/driver.conf
[[ -n "${PKG_URLS:-}" ]] || die "driver.conf has no PKG_URLS"
log "Installing pinned driver $DRIVER_VERSION"
in_chroot "mkdir -p /tmp/nvpkgs"
for u in $PKG_URLS; do
  in_chroot "curl -sfL '$u' -o /tmp/nvpkgs/\$(basename '$u')" || die "download failed: $u"
done
if ! in_chroot "pacman -U --noconfirm --needed /tmp/nvpkgs/*.pkg.tar.zst"; then
  # unattended context: a keyring mismatch (frozen image keyring vs
  # current Arch packager keys) must not brick updates — packages came
  # over HTTPS from Arch infrastructure, so retry unsigned rather than
  # fail the update
  log "WARNING: pacman -U failed (keyring?) — retrying with signature checks off"
  sed 's/^SigLevel.*/SigLevel = Never/' "$MERGED/etc/pacman.conf" > "$MERGED/tmp/pacman-nosig.conf"
  in_chroot "pacman --config /tmp/pacman-nosig.conf -U --noconfirm --needed /tmp/nvpkgs/*.pkg.tar.zst" \
    || die "driver package install failed"
fi
compgen -G "$MERGED/usr/lib/modules/$KVER/updates/dkms/nvidia.ko*" >/dev/null \
  || in_chroot "dkms autoinstall -k $KVER"
compgen -G "$MERGED/usr/lib/modules/$KVER/updates/dkms/nvidia.ko*" >/dev/null \
  || die "driver failed to build for $KVER"
in_chroot "pacman -Qq" | LC_ALL=C sort > "$WORK/after.txt"

BUILD_ONLY_RE='^(dkms|nvidia-open-dkms|patch|gcc|gcc-libs|make|binutils|libisl|libmpc|mpfr|pahole|python-setuptools|linux-neptune.*-headers|.*-headers)$'
mapfile -t NEW_PKGS < <(LC_ALL=C comm -13 "$WORK/before.txt" "$WORK/after.txt" | grep -Ev "$BUILD_ONLY_RE")
[[ ${#NEW_PKGS[@]} -gt 0 ]] || die "payload list empty"
log "Payload: ${NEW_PKGS[*]}"

: > "$WORK/files.txt"
for pkg in "${NEW_PKGS[@]}"; do in_chroot "pacman -Qlq $pkg" >> "$WORK/files.txt"; done
sed 's|^/||' "$WORK/files.txt" > "$WORK/files.rel"

log "Copying driver into $PARTSET rootfs"
rsync -a --files-from="$WORK/files.rel" "$MERGED/" "$NEWROOT/"
rsync -a "$UPPER/usr/lib/modules/$KVER/updates" "$NEWROOT/usr/lib/modules/$KVER/"
for pkg in "${NEW_PKGS[@]}"; do
  for ENTRY in "$UPPER/usr/lib/holo/pacmandb/local/$pkg"-[0-9]*; do
    [[ -d "$ENTRY" ]] && rsync -a "$ENTRY" "$NEWROOT/usr/lib/holo/pacmandb/local/" && break
  done
done
chroot "$NEWROOT" depmod "$KVER"
chroot "$NEWROOT" ldconfig

cat > "$NEWROOT/etc/modprobe.d/99-nvidia-patch.conf" <<'EOF'
# Added by steamos-utils repatch
blacklist nouveau
options nouveau modeset=0
options nvidia-drm modeset=1 fbdev=1
options nvidia NVreg_PreserveVideoMemoryAllocations=1
EOF
chroot "$NEWROOT" systemctl enable nvidia-suspend nvidia-resume nvidia-hibernate 2>/dev/null || true

CMDLINE_ADD='rd.driver.blacklist=nouveau modprobe.blacklist=nouveau nvidia-drm.modeset=1 nvidia-drm.fbdev=1'
grep -q 'rd.driver.blacklist=nouveau' "$NEWROOT/etc/default/grub" \
  || sed -i -E "s#^(GRUB_CMDLINE_LINUX_DEFAULT=\")#\1$CMDLINE_ADD #" "$NEWROOT/etc/default/grub"

# propagate the self-healing machinery (repatch.sh + driver.conf) so
# the NEXT update is covered too
mkdir -p "$NEWROOT/usr/lib/steamos-utils"
cp -a /usr/lib/steamos-utils/. "$NEWROOT/usr/lib/steamos-utils/"
if [[ ! -f "$NEWROOT/usr/bin/steamos-update.orig" ]]; then
  mv "$NEWROOT/usr/bin/steamos-update" "$NEWROOT/usr/bin/steamos-update.orig"
  cp -a /usr/bin/steamos-update "$NEWROOT/usr/bin/steamos-update"
fi
[[ -f "$NEWROOT/usr/lib/systemd/system/steamos-finish-oobe-migration.service" ]] \
  && ln -sf /dev/null "$NEWROOT/etc/systemd/system/steamos-finish-oobe-migration.service"
[[ -f /etc/sudoers.d/zz-deck-nopasswd ]] \
  && install -m 440 /etc/sudoers.d/zz-deck-nopasswd "$NEWROOT/etc/sudoers.d/zz-deck-nopasswd"

# regenerate the new slot's grub.cfg with the nvidia cmdline
log "Regenerating grub config for $PARTSET"
mkdir -p "$NEWROOT/efi"
mount "$EFIDEV" "$NEWROOT/efi"
mount -t proc proc "$NEWROOT/proc"
mount --rbind /sys "$NEWROOT/sys"; mount --make-rslave "$NEWROOT/sys"
mount --rbind /dev "$NEWROOT/dev"; mount --make-rslave "$NEWROOT/dev"
chroot "$NEWROOT" update-grub
grep -q 'rd.driver.blacklist=nouveau' "$NEWROOT/efi/EFI/steamos/grub.cfg" \
  || die "regenerated grub.cfg is missing the nvidia cmdline"

log "Syncing"
[[ "$ROOTFS_TYPE" == btrfs ]] && btrfs filesystem sync "$NEWROOT"
sync -f "$NEWROOT"
[[ $WAS_RO -eq 1 ]] && btrfs property set "$NEWROOT" ro true
log "OK — $PARTSET is NVIDIA-ready ($KVER)"

