#!/usr/bin/env python3

# Copyright (c) 2020-2021 Fpemud <fpemud@sina.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.


import os
import sys
import abc
import pkgutil


def get_kernel_installer(name, settings, **kwargs):
    for mod in pkgutil.iter_modules(["._exporter"]):
        if mod.KernelInstallerImpl.name == name:
            return mod.KernelInstallerImpl(settings, **kwargs)
    assert False        


class KernelInstaller(abc.ABC):

    @classmethod
    @property
    def name(cls):
        fn = sys.modules.get(cls.__module__).__file__
        fn = os.path.basename(fn).replace(".py", "")
        return fn.replace("_", "-")

    @abc.abstractmethod
    def set_program_name(program_name):
        pass

    @abc.abstractmethod
    def set_host_computing_power(host_computing_power):
        pass

    @abc.abstractmethod
    def set_work_dir(work_dir):
        pass

    @abc.abstractmethod
    def check(self):
        pass

    @abc.abstractmethod
    def make(self):
        pass
