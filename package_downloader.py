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

    file: package_downloader.py
'''

"""Shared, hardened package-file downloader used across this project."""

import os
import shutil
import socket
import urllib.parse
import urllib.request
from pathlib import Path

socket.setdefaulttimeout(10)


class PackageDownloader:
    """Downloads a package file into a local cache directory.

    Downloads are atomic (streamed to a ".part" sibling of the
    destination, then renamed into place, so a killed or failed download
    never leaves a corrupt file sitting at the final path) and
    cache-aware (skips re-downloading a file that's already present and
    non-empty, so callers can call download() more than once for the same
    package without re-fetching it).

    Pulled out of NvidiaUsbImageBuilder (this was originally the download
    half of its fetch_pins() method, used to fetch pinned NVIDIA driver
    packages from the Arch archive) into its own shared class so other
    tools in this project that need to fetch a package file use the exact
    same, already-hardened download path instead of a second
    implementation that could drift. AcpiEnabler is the other current
    user — it resolves ITS OWN download URL differently (from the live
    system's pacman.conf/mirrorlist, matching NvidiaUsbImageBuilder's
    resolve_headers_url() approach, not the Arch-archive pin_pkg()
    resolution NVIDIA driver packages use, since Valve's own SteamOS
    kernel packages aren't published on archlinux.org/archive.
    archlinux.org at all), but both hand the resolved URL to THIS class
    to actually fetch it.

    This class intentionally knows nothing about WHERE a URL came from —
    it only downloads it. Resolving a package name/spec into a URL is the
    caller's job (see NvidiaUsbImageBuilder.pin_pkg() and
    AcpiEnabler._resolve_kernel_package_url()).

    log/warn are optional caller-supplied logging hooks so a caller with
    its own styled logging (NvidiaUsbImageBuilder's colored [nvidia-usb]
    lines, AcpiEnabler's own) can plug straight into it instead of
    getting a second, differently-formatted stream of output.

    Attributes:
        dest_dir: Path to the local cache directory downloads land in.
        verbose: Whether the default log hook actually prints anything.
    """

    def __init__(self, dest_dir, verbose=False, log=None, warn=None):
        """Initialize the downloader.

        Args:
            dest_dir: Local cache directory to download files into.
            verbose: If True (and no log hook is given), the default log
                hook prints progress messages.
            log: Optional callable(message) used for informational
                logging instead of the default.
            warn: Optional callable(message) used for warning logging
                instead of the default.
        """
        self.dest_dir = Path(dest_dir)
        self.verbose = verbose
        self._log_fn = log or (lambda message: print(message) if self.verbose else None)
        self._warn_fn = warn or (lambda message: print('WARNING: %s' % message))

    def url_exists(self, url):
        """Check whether url responds with a 2xx status.

        Args:
            url: The URL to probe with an HTTP HEAD request.

        Returns:
            True only on a 2xx response; False on anything else,
            including network errors.
        """
        try:
            req = urllib.request.Request(url, method='HEAD')
            with urllib.request.urlopen(req) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    def download(self, url, filename=None):
        """Download url into dest_dir, using the local cache if possible.

        Args:
            url: The URL to download.
            filename: Destination filename under dest_dir. Defaults to
                the URL's own basename.

        Returns:
            The local Path the file was downloaded (or already cached)
            to.

        Raises:
            RuntimeError: If filename cannot be determined, or the
                download itself fails.
        """
        filename = filename or os.path.basename(urllib.parse.urlsplit(url).path)
        if not filename:
            raise RuntimeError('could not determine a filename from %s' % url)

        self.dest_dir.mkdir(parents=True, exist_ok=True)
        dest = self.dest_dir / filename

        if dest.exists() and dest.stat().st_size > 0:
            self._log_fn('Cached: %s' % filename)
            return dest

        self._log_fn('Downloading %s' % filename)

        part = dest.with_suffix(dest.suffix + '.part')
        try:
            with urllib.request.urlopen(url) as resp, open(part, 'wb') as f:
                shutil.copyfileobj(resp, f)
            part.rename(dest)
        except Exception as exc:
            raise RuntimeError('download failed: %s (%s)' % (url, exc)) from exc
        return dest
