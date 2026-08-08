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

    file: legion_go2_brightness_slider.py
'''

"""Legion Go 2 brightness slider and color-correction fix.

Automates the gamescope-script fix described in this Reddit post:
https://www.reddit.com/r/LegionGo/comments/1s4mhlu/legion_go_2_steamos_display_fixes_color_banding/

Restored from the pre-refactor LegionGo2BrightnessSlider.py (see git
history: commit 533df96 added it, 527744d wired it up, 678eddc's
refactor accidentally left only a stub behind). The fix itself is
unchanged -- same gamescope Lua script, same target path -- just ported
onto this project's current conventions: a plain subprocess call
instead of the retired ShellUtils.run_command()/dry_run parameter (no
current caller ever passed dry_run=False, so the whole module was
effectively a no-op), and pathlib in place of the old os.path calls to
match the rest of the codebase (acpi_enabler.py, nvidia_usb_image_builder.py).
"""

from pathlib import Path

# Directory gamescope reads custom per-display Lua scripts from.
SCRIPTS_DIR = Path('/home/deck/.config/gamescope/scripts')

# Registers the Legion Go 2's OLED panel (AMS881KB01-0) with gamescope:
# its colorimetry (for correct color reproduction instead of the
# washed-out/oversaturated default) and its two supported dynamic
# refresh rates (60/144Hz), which is what actually exposes a working
# brightness slider for this panel in gamescope's quick-access menu.
BRIGHTNESS_SLIDER_AND_COLOR_CORRECTION_SCRIPT = '''\
local lenovo_go2_oled_colorimetry = {
  r = { x = 0.6835, y = 0.3154 },
  g = { x = 0.2402, y = 0.7138 },
  b = { x = 0.1396, y = 0.0439 },
  w = { x = 0.3134, y = 0.3291 },
}

gamescope.config.known_displays.lenovo_go2_oled = {
  pretty_name = "AMS881KB01-0 OLED",
  dynamic_refresh_rates = {
    60,
    144,
  },
  hdr = {
    supported = true,
    force_enabled = true,
    eotf = gamescope.eotf.gamma22,
    max_content_light_level = 1107.128,
    max_frame_average_luminance = 475.683,
    min_content_light_level = 0.001,
  },
  colorimetry = lenovo_go2_oled_colorimetry,
  dynamic_modegen = function(base_mode, refresh)
    debug("Generating mode " .. refresh .. "Hz for AMS881KB01-0 OLED")
    local mode = base_mode

    gamescope.modegen.set_resolution(mode, 1920, 1200)
    gamescope.modegen.set_h_timings(mode, 32, 8, 40)
    if refresh == 60 then
      gamescope.modegen.set_v_timings(mode, 1904, 8, 56)
    else
      gamescope.modegen.set_v_timings(mode, 56, 8, 56)
    end
    mode.clock = gamescope.modegen.calc_max_clock(mode, refresh)
    mode.vrefresh = gamescope.modegen.calc_vrefresh(mode)

    return mode
  end,
  matches = function(display)
    if display.vendor == "SDC" and display.product == 17153 then
      return 5000
    end
    return -1
  end,
}
debug("Registered AMS881KB01-0 OLED as a known display")
'''

_SCRIPT_FILENAME = SCRIPTS_DIR / 'lenovo.legiongo2.oled.lua'


def enable_lego2_brightness_slider():
    """Enable the Legion Go 2 brightness slider and color-correction fix.

    Writes the gamescope known-display Lua script that registers the
    Legion Go 2's OLED panel (colorimetry + dynamic refresh rates),
    which is what makes gamescope expose a working brightness slider
    and correct colors for this panel.

    Returns:
        True on success, False if the scripts directory could not be
        created or the script could not be written.
    """
    print('\nNow creating gamescope scripts directory')
    try:
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print('Unable to create gamescope scripts directory: %s (%s)' % (SCRIPTS_DIR, e))
        return False

    if not SCRIPTS_DIR.is_dir():
        print('Unable to create gamescope scripts directory: %s' % SCRIPTS_DIR)
        return False

    print('Gamescope scripts directory: %s successfully created' % SCRIPTS_DIR)

    try:
        _SCRIPT_FILENAME.write_text(BRIGHTNESS_SLIDER_AND_COLOR_CORRECTION_SCRIPT, encoding='utf-8')
        print('Successfully wrote gamescope script to: %s' % _SCRIPT_FILENAME)
    except PermissionError:
        print('Error: Permission denied. Unable to write gamescope script to: %s' % _SCRIPT_FILENAME)
        return False
    except OSError as e:
        print('OS error occurred: %s' % e)
        return False

    return True


def remove_lego2_brightness_slider():
    """Disable and remove the Legion Go 2 brightness slider and color-correction fix.

    Returns:
        True on success (including if the script was already absent),
        False if removal failed.
    """
    print('\nNow removing Legion Go 2 brightness slider and color correction fix')
    try:
        _SCRIPT_FILENAME.unlink(missing_ok=True)
    except OSError as e:
        print('OS error occurred while removing %s: %s' % (_SCRIPT_FILENAME, e))
        return False

    print('Successfully removed gamescope script: %s' % _SCRIPT_FILENAME)
    return True
