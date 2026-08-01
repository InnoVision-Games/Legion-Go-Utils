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

    file: common/selfheal/__init__.py

    Marks common/selfheal/ as a package. Holds the on-device self-heal
    PAYLOAD scripts shared by AcpiEnabler and NvidiaUsbImageBuilder --
    repatch_script.py and update_wrapper.py -- both of which are shipped
    as real standalone files and copied verbatim onto the target
    filesystem (shutil.copy() against the REPATCH_SCRIPT /
    UPDATE_WRAPPER_SCRIPT class constants), never imported as Python
    modules in the normal sense. This is the deliberate counterpart to
    common/lib/, which holds host-side helper modules that ARE imported
    normally -- see common/__init__.py for the split.
'''

"""Package init: common/selfheal/ holds the on-device self-heal payload scripts."""
