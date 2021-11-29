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
import imp
import shutil
import sys

import fasteners

from snakeoil import fileutils

from DeComp.compress import CompressMap

from . import log
from . import support
from .defaults import (SOURCE_MOUNT_DEFAULTS, TARGET_MOUNT_DEFAULTS, PORT_LOGDIR_CLEAN)
from .support import (CatalystError, file_locate, normpath, cmd, read_makeconf, ismount, file_check, addl_arg_parse)
from .fileops import ensure_dirs, clear_dir, clear_path
from .resume import AutoResume


class Builder:
    """
    This class does all of the chroot setup, copying of files, etc. It is
    the driver class for pretty much everything that glivedisk does.
    """

    def get(self, settings):
        pass

    required_values = [
        "storedir",
        "sharedir",
        "distdir",
        "portdir",
        "version_stamp",
        "target",
        "subarch",
        "rel_type",
        "profile",
        "snapshot",
        "source_subpath",
    ]

    valid_values = [
        "portage_confdir",
        "portage_prefix",
        "portage_overlay",
        "cflags",
        "cxxflags",
        "fcflags",
        "fflags",
        "ldflags",
        "asflags",
        "common_flags",
        "cbuild",
        "hostuse",
        "catalyst_use",
        "distcc_hosts",
        "makeopts",
        "pkgcache_path",
        "kerncache_path",
        "compression_mode",
        "decompression_mode",
        "interpreter",
        "fstype",
        "fsopts",
        "options",
        "snapshot_cache",
        "hash_function",
        "digests",
        "contents",
        "compressor_arch",
        "compression_mode",
        "compressor_options",
        "decompressor_search_order",
    ]

    def __init__(self, settings):
        self.set_valid_build_kernel_vars(settings)
        self._addl_arg_parse(settings)

        self.settings = settings
        self.env = {
            'PATH': '/bin:/sbin:/usr/bin:/usr/sbin',
            'TERM': os.getenv('TERM', 'dumb'),
        }

        self.resume = None

        self.makeconf = {}

        self.archmap = {}
        self.subarchmap = {}

        machinemap = {}

        log.debug("Begin loading arch modules...")
        for x in support.get_modules(self.settings["archdir"]):
            log.debug("\tLoading %s", x)
            try:
                with open(os.path.join(self.settings["archdir"], x + ".py")) as fh:
                    # This next line loads the plugin as a module and assigns it to archmap[x]
                    self.archmap[x] = imp.load_module(x, fh, os.path.join(self.settings["archdir"], x + ".py"), (".py", "r", imp.PY_SOURCE))
                    # This next line registers all the subarches supported in the plugin
                    tmpsubarchmap, tmpmachinemap = self.archmap[x].register()
                    self.subarchmap.update(tmpsubarchmap)
                    for machine in tmpmachinemap:
                        machinemap[machine] = x
                    for subarch in tmpsubarchmap:
                        machinemap[subarch] = x
            except IOError:
                # This message should probably change a bit, since everything in
                # the dir should load just fine. If it doesn't, it's probably a
                # syntax error in the module
                log.warning("Can't find/load %s.py plugin in %s", x, self.settings["archdir"])
            log.debug("Loaded arch module: %s", self.archmap[x])

        if "chost" in self.settings:
            hostmachine = self.settings["chost"].split("-")[0]
            if hostmachine not in machinemap:
                raise CatalystError("Unknown host machine type " + hostmachine)
            self.settings["hostarch"] = machinemap[hostmachine]
        else:
            hostmachine = self.settings["subarch"]
            if hostmachine in machinemap:
                hostmachine = machinemap[hostmachine]
            self.settings["hostarch"] = hostmachine
        if "cbuild" in self.settings:
            buildmachine = self.settings["cbuild"].split("-")[0]
        else:
            buildmachine = os.uname()[4]
        if buildmachine not in machinemap:
            raise CatalystError("Unknown build machine type " + buildmachine)
        self.settings["buildarch"] = machinemap[buildmachine]
        self.settings["crosscompile"] = (self.settings["hostarch"] != self.settings["buildarch"])

        # Call arch constructor, pass our settings
        try:
            self.arch = self.subarchmap[self.settings["subarch"]](self.settings)
        except KeyError:
            log.critical(
                'Invalid subarch: %s\n'
                'Choose one of the following:\n'
                ' %s',
                self.settings['subarch'], ' '.join(self.subarchmap))

        log.notice('Using target: %s', self.settings['target'])
        # Print a nice informational message
        if self.settings["buildarch"] == self.settings["hostarch"]:
            log.info('Building natively for %s', self.settings['hostarch'])
        elif self.settings["crosscompile"]:
            log.info('Cross-compiling on %s for different machine type %s',
                self.settings['buildarch'], self.settings['hostarch'])
        else:
            log.info('Building on %s for alternate personality type %s',
                self.settings['buildarch'], self.settings['hostarch'])

        # This must be set first as other set_ options depend on this
        self.set_spec_prefix()

        # Initialize our (de)compressor's)
        self.decompressor = CompressMap(self.settings["decompress_definitions"],
            env = self.env,
            search_order = self.settings["decompressor_search_order"],
            comp_prog = self.settings["comp_prog"],
            decomp_opt = self.settings["decomp_opt"])
        self.accepted_extensions = self.decompressor.search_order_extensions(self.settings["decompressor_search_order"])
        log.notice("Source file specification matching setting is: %s", self.settings["source_matching"])
        log.notice("Accepted source file extensions search order: %s", self.accepted_extensions)
        # save resources, it is not always needed
        self.compressor = None

        # Define all of our core variables
        self.set_target_subpath()
        self.set_source_subpath()

        # Set paths
        self.set_snapshot_path()
        self.set_root_path()
        self.set_source_path()
        self.set_snapcache_path()
        self.set_chroot_path()
        self.set_autoresume_path()
        self.set_dest_path()
        self.set_stage_path()
        self.set_target_path()

        self.set_controller_file()
        self.set_default_action_sequence()
        self.set_use()
        self.set_catalyst_use()
        self.set_cleanables()
        self.set_build_kernel_vars()
        self.set_install_mask()
        self.set_rcadd()
        self.set_rcdel()
        self.set_cdtar()
        self.set_packages()
        self.set_rm()
        self.set_linuxrc()
        self.set_overlay()
        self.set_portage_overlay()
        self.set_root_overlay()

        # This next line checks to make sure that the specified variables exist on disk.
        #pdb.set_trace()
        file_locate(self.settings, ["distdir"], expand = 0)
        # If we are using portage_confdir, check that as well.
        if "portage_confdir" in self.settings:
            file_locate(self.settings, ["portage_confdir"], expand = 0)

        # Setup our mount points.
        # initialize our target mounts.
        self.target_mounts = TARGET_MOUNT_DEFAULTS.copy()

        self.mounts = ["proc", "dev", "portdir", "distdir", "port_tmpdir"]
        # initialize our source mounts
        self.mountmap = SOURCE_MOUNT_DEFAULTS.copy()
        # update these from settings
        self.mountmap["portdir"] = self.settings["portdir"]
        self.mountmap["distdir"] = self.settings["distdir"]
        self.target_mounts["portdir"] = support.normpath(self.settings["repo_basedir"] + "/" + self.settings["repo_name"])
        self.target_mounts["distdir"] = self.settings["target_distdir"]
        self.target_mounts["packagedir"] = self.settings["target_pkgdir"]
        if "snapcache" not in self.settings["options"]:
            self.mounts.remove("portdir")
            self.mountmap["portdir"] = None
        else:
            self.mountmap["portdir"] = support.normpath("/".join([
                self.settings["snapshot_cache_path"],
                self.settings["repo_name"],
                ]))
        if os.uname()[0] == "Linux":
            self.mounts.append("devpts")
            self.mounts.append("shm")
            self.mounts.append("run")

        self.set_mounts()

        # Configure any user specified options (either in catalyst.conf or on the command line).
        if "pkgcache" in self.settings["options"]:
            self.set_pkgcache_path()
            log.info('Location of the package cache is %s', self.settings['pkgcache_path'])
            self.mounts.append("packagedir")
            self.mountmap["packagedir"] = self.settings["pkgcache_path"]

        if "kerncache" in self.settings["options"]:
            self.set_kerncache_path()
            log.info('Location of the kerncache is %s', self.settings['kerncache_path'])
            self.mounts.append("kerncache")
            self.mountmap["kerncache"] = self.settings["kerncache_path"]

        if "port_logdir" in self.settings:
            self.mounts.append("port_logdir")
            self.mountmap["port_logdir"] = self.settings["port_logdir"]
            self.env["PORT_LOGDIR"] = self.settings["port_logdir"]
            self.env["PORT_LOGDIR_CLEAN"] = PORT_LOGDIR_CLEAN

    def set_install_mask(self):
        if "install_mask" in self.settings:
            if not isinstance(self.settings['install_mask'], str):
                self.settings["install_mask"] = \
                    ' '.join(self.settings["install_mask"])

    def set_spec_prefix(self):
        self.settings["spec_prefix"] = self.settings["target"]

    def set_target_subpath(self):
        common = self.settings["rel_type"] + "/" + self.settings["target"] + "-" + self.settings["subarch"]
        self.settings["target_subpath"] = common + "-" + self.settings["version_stamp"] + "/"
        self.settings["target_subpath_unversioned"] = common + "/"

    def set_source_subpath(self):
        if not isinstance(self.settings['source_subpath'], str):
            raise CatalystError(
                "source_subpath should have been a string. Perhaps you have " + \
                "something wrong in your spec file?")

    def set_pkgcache_path(self):
        if "pkgcache_path" in self.settings:
            if not isinstance(self.settings['pkgcache_path'], str):
                self.settings["pkgcache_path"] = \
                    support.normpath(self.settings["pkgcache_path"])
        elif "versioned_cache" in self.settings["options"]:
            self.settings["pkgcache_path"] = \
                support.normpath(self.settings["storedir"] + "/packages/" + \
                self.settings["target_subpath"] + "/")
        else:
            self.settings["pkgcache_path"] = \
                support.normpath(self.settings["storedir"] + "/packages/" + \
                self.settings["target_subpath_unversioned"] + "/")

    def set_kerncache_path(self):
        if "kerncache_path" in self.settings:
            if not isinstance(self.settings['kerncache_path'], str):
                self.settings["kerncache_path"] = \
                    support.normpath(self.settings["kerncache_path"])
        elif "versioned_cache" in self.settings["options"]:
            self.settings["kerncache_path"] = support.normpath(self.settings["storedir"] + \
                "/kerncache/" + self.settings["target_subpath"])
        else:
            self.settings["kerncache_path"] = support.normpath(self.settings["storedir"] + \
                "/kerncache/" + self.settings["target_subpath_unversioned"])

    def set_target_path(self):
        self.settings["target_path"] = support.normpath(os.path.join(self.settings["storedir"], "builds", self.settings["target_subpath"]))
        if "autoresume" in self.settings["options"] and self.resume.is_enabled("setup_target_path"):
            log.notice('Resume point detected, skipping target path setup operation...')
        else:
            self.resume.enable("setup_target_path")
            ensure_dirs(os.path.join(self.settings["storedir"] + "builds"))

    def set_rcadd(self):
        if self.settings["spec_prefix"] + "/rcadd" in self.settings:
            self.settings["rcadd"] = \
                self.settings[self.settings["spec_prefix"] + "/rcadd"]
            del self.settings[self.settings["spec_prefix"] + "/rcadd"]

    def set_rcdel(self):
        if self.settings["spec_prefix"] + "/rcdel" in self.settings:
            self.settings["rcdel"] = \
                self.settings[self.settings["spec_prefix"] + "/rcdel"]
            del self.settings[self.settings["spec_prefix"] + "/rcdel"]

    def set_cdtar(self):
        if self.settings["spec_prefix"] + "/cdtar" in self.settings:
            self.settings["cdtar"] = \
                support.normpath(self.settings[self.settings["spec_prefix"] + "/cdtar"])
            del self.settings[self.settings["spec_prefix"] + "/cdtar"]

    def set_source_path(self):
        if "seedcache" in self.settings["options"] and os.path.isdir(support.normpath(self.settings["storedir"] + "/tmp/" + self.settings["source_subpath"] + "/")):
            self.settings["source_path"] = support.normpath(self.settings["storedir"] + "/tmp/" + self.settings["source_subpath"] + "/")
            log.debug("source_subpath is: %s", self.settings["source_path"])
        else:
            log.debug('Checking source path existence and get the final filepath. subpath: %s', self.settings["source_subpath"])
            self.settings["source_path"] = file_check(
                support.normpath(self.settings["storedir"] + "/builds/" +
                    self.settings["source_subpath"]),
                self.accepted_extensions,
                self.settings["source_matching"] in ["strict"]
                )
            log.debug('Source path returned from file_check is: %s',
                self.settings["source_path"])
            if os.path.isfile(self.settings["source_path"]):
                # XXX: Is this even necessary if the previous check passes?
                if os.path.exists(self.settings["source_path"]):
                    self.settings["source_path_hash"] = \
                        self.settings["hash_map"].generate_hash(
                            self.settings["source_path"],
                            hash_ = self.settings["hash_function"])
        log.notice('Source path set to %s', self.settings['source_path'])

    def set_dest_path(self):
        if "root_path" in self.settings:
            self.settings["destpath"] = support.normpath(self.settings["chroot_path"] + self.settings["root_path"])
        else:
            self.settings["destpath"] = support.normpath(self.settings["chroot_path"])

    def set_cleanables(self):
        self.settings["cleanables"] = ["/etc/machine-id", "/etc/resolv.conf", "/var/tmp/*", "/tmp/*",
            self.settings["repo_basedir"] + "/" +
            self.settings["repo_name"]]

    def set_snapshot_path(self):
        self.settings["snapshot_path"] = file_check(
            support.normpath(self.settings["storedir"] +
                "/snapshots/" + self.settings["snapshot_name"] +
                self.settings["snapshot"]),
            self.accepted_extensions,
            self.settings["source_matching"] is "strict"
            )
        log.info('SNAPSHOT_PATH set to: %s', self.settings['snapshot_path'])
        self.settings["snapshot_path_hash"] = \
            self.settings["hash_map"].generate_hash(
                self.settings["snapshot_path"],
                hash_ = self.settings["hash_function"])

    def set_snapcache_path(self):
        self.settings["snapshot_cache_path"] = \
            support.normpath(os.path.join(self.settings["snapshot_cache"],
                self.settings["snapshot"]))
        if "snapcache" in self.settings["options"]:
            self.settings["snapshot_cache_path"] = \
                support.normpath(os.path.join(self.settings["snapshot_cache"],
                    self.settings["snapshot"]))
            log.info('Setting snapshot cache to %s', self.settings['snapshot_cache_path'])

    def set_chroot_path(self):
        """
        NOTE: the trailing slash has been removed
        Things *could* break if you don't use a proper join()
        """
        self.settings["chroot_path"] = support.normpath(self.settings["storedir"] + "/tmp/" + self.settings["target_subpath"].rstrip('/'))

    def set_autoresume_path(self):
        self.settings["autoresume_path"] = support.normpath(os.path.join(self.settings["storedir"], "tmp", self.settings["rel_type"],
            ".autoresume-%s-%s-%s"
            %(self.settings["target"], self.settings["subarch"],
                self.settings["version_stamp"])
            ))
        if "autoresume" in self.settings["options"]:
            log.info('The autoresume path is %s', self.settings['autoresume_path'])
        self.resume = AutoResume(self.settings["autoresume_path"], mode=0o755)

    def set_controller_file(self):
        self.settings["controller_file"] = support.normpath(self.settings["sharedir"] +
            "/targets/" + self.settings["target"] + "/" + self.settings["target"]
            + "-controller.sh")

    def set_default_action_sequence(self):
        """ Default action sequence for run method.

        This method sets the optional purgeonly action sequence and returns.
        Or it calls the normal set_action_sequence() for the target stage.
        """
        if "purgeonly" in self.settings["options"]:
            self.settings["action_sequence"] = ["remove_chroot"]
            return
        self.set_action_sequence()

    def set_action_sequence(self):
        """Set basic stage1, 2, 3 action sequences"""
        self.settings["action_sequence"] = ["unpack", "unpack_snapshot",
                "setup_confdir", "portage_overlay",
                "base_dirs", "bind", "chroot_setup", "setup_environment",
                "run_local", "preclean", "unbind", "clean"]
        self.set_completion_action_sequences()

    def set_completion_action_sequences(self):
        if "fetch" not in self.settings["options"]:
            self.settings["action_sequence"].append("capture")
        if "keepwork" in self.settings["options"]:
            self.settings["action_sequence"].append("clear_autoresume")
        elif "seedcache" in self.settings["options"]:
            self.settings["action_sequence"].append("remove_autoresume")
        else:
            self.settings["action_sequence"].append("remove_autoresume")
            self.settings["action_sequence"].append("remove_chroot")
        return

    def set_use(self):
        use = self.settings["spec_prefix"] + "/use"
        if use in self.settings:
            if isinstance(self.settings[use], str):
                self.settings["use"] = self.settings[use].split()
            else:
                self.settings["use"] = self.settings[use]
            del self.settings[use]
        else:
            self.settings["use"] = []

    def set_catalyst_use(self):
        catalyst_use = self.settings["spec_prefix"] + "/catalyst_use"
        if catalyst_use in self.settings:
            if isinstance(self.settings[catalyst_use], str):
                self.settings["catalyst_use"] = self.settings[catalyst_use].split()
            else:
                self.settings["catalyst_use"] = self.settings[catalyst_use]
            del self.settings[catalyst_use]
        else:
            self.settings["catalyst_use"] = []

        # Force bindist when options ask for it
        if "bindist" in self.settings["options"]:
            log.debug("Enabling bindist USE flag")
            self.settings["catalyst_use"].append("bindist")

    def set_stage_path(self):
        self.settings["stage_path"] = support.normpath(self.settings["chroot_path"])

    def set_mounts(self):
        pass

    def set_packages(self):
        pass

    def set_rm(self):
        if self.settings["spec_prefix"] + "/rm" in self.settings:
            if isinstance(self.settings[self.settings['spec_prefix'] + '/rm'], str):
                self.settings[self.settings["spec_prefix"] + "/rm"] = \
                    self.settings[self.settings["spec_prefix"] + "/rm"].split()

    def set_linuxrc(self):
        if self.settings["spec_prefix"] + "/linuxrc" in self.settings:
            if isinstance(self.settings[self.settings['spec_prefix'] + '/linuxrc'], str):
                self.settings["linuxrc"] = \
                    self.settings[self.settings["spec_prefix"] + "/linuxrc"]
                del self.settings[self.settings["spec_prefix"] + "/linuxrc"]

    def set_portage_overlay(self):
        if "portage_overlay" in self.settings:
            if isinstance(self.settings['portage_overlay'], str):
                self.settings["portage_overlay"] = \
                    self.settings["portage_overlay"].split()
            log.info('portage_overlay directories are set to: %s',
                ' '.join(self.settings['portage_overlay']))

    def set_overlay(self):
        if self.settings["spec_prefix"] + "/overlay" in self.settings:
            if isinstance(self.settings[self.settings['spec_prefix'] + '/overlay'], str):
                self.settings[self.settings["spec_prefix"] + "/overlay"] = \
                    self.settings[self.settings["spec_prefix"] + \
                    "/overlay"].split()

    def set_root_overlay(self):
        if self.settings["spec_prefix"] + "/root_overlay" in self.settings:
            if isinstance(self.settings[self.settings['spec_prefix'] + '/root_overlay'], str):
                self.settings[self.settings["spec_prefix"] + "/root_overlay"] = \
                    self.settings[self.settings["spec_prefix"] + \
                    "/root_overlay"].split()

    def set_root_path(self):
        """ ROOT= variable for emerges """
        self.settings["root_path"] = "/"

    def set_valid_build_kernel_vars(self,addlargs):
        if "boot/kernel" in addlargs:
            if isinstance(addlargs['boot/kernel'], str):
                loopy = [addlargs["boot/kernel"]]
            else:
                loopy = addlargs["boot/kernel"]

            for x in loopy:
                self.valid_values.append("boot/kernel/" + x + "/aliases")
                self.valid_values.append("boot/kernel/" + x + "/config")
                self.valid_values.append("boot/kernel/" + x + "/console")
                self.valid_values.append("boot/kernel/" + x + "/extraversion")
                self.valid_values.append("boot/kernel/" + x + "/gk_action")
                self.valid_values.append("boot/kernel/" + x + "/gk_kernargs")
                self.valid_values.append("boot/kernel/" + x + "/initramfs_overlay")
                self.valid_values.append("boot/kernel/" + x + "/machine_type")
                self.valid_values.append("boot/kernel/" + x + "/sources")
                self.valid_values.append("boot/kernel/" + x + "/softlevel")
                self.valid_values.append("boot/kernel/" + x + "/use")
                self.valid_values.append("boot/kernel/" + x + "/packages")
                self.valid_values.append("boot/kernel/" + x + "/kernelopts")
                if "boot/kernel/" + x + "/packages" in addlargs:
                    if isinstance(addlargs['boot/kernel/' + x + '/packages'], str):
                        addlargs["boot/kernel/" + x + "/packages"] = \
                            [addlargs["boot/kernel/" + x + "/packages"]]

    def set_build_kernel_vars(self):
        if self.settings["spec_prefix"] + "/gk_mainargs" in self.settings:
            self.settings["gk_mainargs"] = \
                self.settings[self.settings["spec_prefix"] + "/gk_mainargs"]
            del self.settings[self.settings["spec_prefix"] + "/gk_mainargs"]

    def kill_chroot_pids(self):
        log.info('Checking for processes running in chroot and killing them.')

        # Force environment variables to be exported so script can see them
        self.setup_environment()

        killcmd = support.normpath(self.settings["sharedir"] +
            self.settings["shdir"] + "/support/kill-chroot-pids.sh")
        if os.path.exists(killcmd):
            cmd([killcmd], env = self.env)

    def mount_safety_check(self):
        """
        Check and verify that none of our paths in mypath are mounted. We don't
        want to clean up with things still mounted, and this allows us to check.
        Returns 1 on ok, 0 on "something is still mounted" case.
        """

        if not os.path.exists(self.settings["chroot_path"]):
            return

        log.debug('self.mounts = %s', self.mounts)
        for x in self.mounts:
            target = support.normpath(self.settings["chroot_path"] + self.target_mounts[x])
            log.debug('mount_safety_check() x = %s %s', x, target)
            if not os.path.exists(target):
                continue

            if ismount(target):
                # Something is still mounted
                try:
                    log.warning('%s is still mounted; performing auto-bind-umount...', target)
                    # Try to umount stuff ourselves
                    self.unbind()
                    if ismount(target):
                        raise CatalystError("Auto-unbind failed for " + target)
                    else:
                        log.notice('Auto-unbind successful...')
                except CatalystError:
                    raise CatalystError("Unable to auto-unbind " + target)

    def unpack(self):

        clst_unpack_hash = self.resume.get("unpack")

        # Set up all unpack info settings
        unpack_info = self.decompressor.create_infodict(
            source = self.settings["source_path"],
            destination = self.settings["chroot_path"],
            arch = self.settings["compressor_arch"],
            other_options = self.settings["compressor_options"],
            )

        display_msg = (
            'Starting %(mode)s from %(source)s\nto '
            '%(destination)s (this may take some time) ..')

        error_msg = "'%(mode)s' extraction of %(source)s to %(destination)s failed."

        if "seedcache" in self.settings["options"]:
            if os.path.isdir(unpack_info["source"]):
                # SEEDCACHE Is a directory, use rsync
                unpack_info['mode'] = "rsync"
            else:
                # SEEDCACHE is a not a directory, try untar'ing
                log.notice('Referenced SEEDCACHE does not appear to be a directory, trying to untar...')
                unpack_info['source'] = file_check(unpack_info['source'])
        else:
            # No SEEDCACHE, use tar
            unpack_info['source'] = file_check(unpack_info['source'])
        # end of unpack_info settings

        # set defaults,
        # only change them if the resume point is proven to be good
        _unpack = True
        invalid_chroot = True
        # Begin autoresume validation
        if "autoresume" in self.settings["options"]:
            # check chroot
            if os.path.isdir(self.settings["chroot_path"]):
                if self.resume.is_enabled("unpack"):
                    # Autoresume is valid in the chroot
                    _unpack = False
                    invalid_chroot = False
                    log.notice('Resume: "chroot" is valid...')
                else:
                    # self.resume.is_disabled("unpack")
                    # Autoresume is invalid in the chroot
                    log.notice('Resume: "seed source" unpack resume point is disabled')

            # check seed source
            if os.path.isfile(self.settings["source_path"]) and not invalid_chroot:
                if self.settings["source_path_hash"].replace("\n", " ") == clst_unpack_hash:
                    # Seed tarball has not changed, chroot is valid
                    _unpack = False
                    invalid_chroot = False
                    log.notice('Resume: "seed source" hash matches chroot...')
                else:
                    # self.settings["source_path_hash"] != clst_unpack_hash
                    # Seed tarball has changed, so invalidate the chroot
                    _unpack = True
                    invalid_chroot = True
                    log.notice('Resume: "seed source" has changed, hashes do not match, invalidating resume...')
                    log.notice('        source_path......: %s', self.settings["source_path"])
                    log.notice('        new source hash..: %s', self.settings["source_path_hash"].replace("\n", " "))
                    log.notice('        recorded hash....: %s', clst_unpack_hash)
                    unpack_info['source'] = file_check(unpack_info['source'])

        else:
            # No autoresume, check SEEDCACHE
            if "seedcache" in self.settings["options"]:
                # if the seedcache is a dir, rsync will clean up the chroot
                if os.path.isdir(self.settings["source_path"]):
                    pass
            elif os.path.isdir(self.settings["source_path"]):
                    # We should never reach this, so something is very wrong
                    raise CatalystError(
                        "source path is a dir but seedcache is not enabled: %s"
                        % self.settings["source_path"])

        if _unpack:
            self.mount_safety_check()

            if invalid_chroot:
                if "autoresume" in self.settings["options"]:
                    log.notice('Resume: Target chroot is invalid, cleaning up...')

                self.clear_autoresume()
                self.clear_chroot()

            ensure_dirs(self.settings["chroot_path"])

            ensure_dirs(self.settings["chroot_path"] + "/tmp", mode=1777)

            if "pkgcache" in self.settings["options"]:
                ensure_dirs(self.settings["pkgcache_path"], mode=0o755)

            if "kerncache" in self.settings["options"]:
                ensure_dirs(self.settings["kerncache_path"], mode=0o755)

            log.notice('%s', display_msg % unpack_info)

            # now run the decompressor
            if not self.decompressor.extract(unpack_info):
                log.error('%s', error_msg % unpack_info)

            if "source_path_hash" in self.settings:
                self.resume.enable("unpack",
                    data = self.settings["source_path_hash"])
            else:
                self.resume.enable("unpack")
        else:
            log.notice('Resume: Valid resume point detected, skipping seed unpack operation...')

    def unpack_snapshot(self):
        unpack = True
        snapshot_hash = self.resume.get("unpack_repo")

        unpack_errmsg = "Error unpacking snapshot using mode %(mode)s"

        unpack_info = self.decompressor.create_infodict(
            source = self.settings["snapshot_path"],
            destination = self.settings["snapshot_cache_path"],
            arch = self.settings["compressor_arch"],
            other_options = self.settings["compressor_options"],
            )

        target_portdir = support.normpath(self.settings["chroot_path"] +
            self.settings["repo_basedir"] + "/" + self.settings["repo_name"])
        log.info('%s', self.settings['chroot_path'])
        log.info('unpack_snapshot(), target_portdir = %s', target_portdir)
        if "snapcache" in self.settings["options"]:
            snapshot_cache_hash_path = os.path.join(
                self.settings['snapshot_cache_path'], 'catalyst-hash')
            snapshot_cache_hash = fileutils.readfile(snapshot_cache_hash_path, True)
            unpack_info['mode'] = self.decompressor.determine_mode(
                unpack_info['source'])

            cleanup_msg = "Cleaning up invalid snapshot cache at \n\t" + \
                self.settings["snapshot_cache_path"] + \
                " (this can take a long time)..."

            if self.settings["snapshot_path_hash"] == snapshot_cache_hash:
                log.info('Valid snapshot cache, skipping unpack of portage tree...')
                unpack = False
        else:
            cleanup_msg = \
                'Cleaning up existing portage tree (this can take a long time)...'
            unpack_info['destination'] = support.normpath(
                self.settings["chroot_path"] + self.settings["repo_basedir"])
            unpack_info['mode'] = self.decompressor.determine_mode(
                unpack_info['source'])

            if "autoresume" in self.settings["options"] \
                and os.path.exists(target_portdir) \
                and self.resume.is_enabled("unpack_repo") \
                and self.settings["snapshot_path_hash"] == snapshot_hash:
                log.notice('Valid Resume point detected, skipping unpack of portage tree...')
                unpack = False

        if unpack:
            if os.path.exists(target_portdir):
                log.info('%s', cleanup_msg)
            clear_dir(target_portdir)

            log.notice('Unpacking portage tree (this can take a long time) ...')
            if not self.decompressor.extract(unpack_info):
                log.error('%s', unpack_errmsg % unpack_info)

            if "snapcache" in self.settings["options"]:
                with open(snapshot_cache_hash_path, 'w') as myf:
                    myf.write(self.settings["snapshot_path_hash"])
            else:
                log.info('Setting snapshot autoresume point')
                self.resume.enable("unpack_repo",
                    data = self.settings["snapshot_path_hash"])

    def config_profile_link(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("config_profile_link"):
            log.notice('Resume point detected, skipping config_profile_link operation...')
        else:
            # TODO: zmedico and I discussed making this a directory and pushing
            # in a parent file, as well as other user-specified configuration.
            log.info('Configuring profile link...')
            clear_path(self.settings['chroot_path'] + \
                self.settings['port_conf'] + '/make.profile')
            ensure_dirs(self.settings['chroot_path'] + self.settings['port_conf'])
            cmd(['ln', '-sf',
                '../..' + self.settings['portdir'] + '/profiles/' + self.settings['profile'],
                self.settings['chroot_path'] + self.settings['port_conf'] + '/make.profile'],
                env=self.env)
            self.resume.enable("config_profile_link")

    def setup_confdir(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("setup_confdir"):
            log.notice('Resume point detected, skipping setup_confdir operation...')
        else:
            if "portage_confdir" in self.settings:
                log.info('Configuring %s...', self.settings['port_conf'])
                dest = support.normpath(self.settings['chroot_path'] + '/' + self.settings['port_conf'])
                ensure_dirs(dest)
                # The trailing slashes on both paths are important:
                # We want to make sure rsync copies the dirs into each
                # other and not as subdirs.
                cmd(['rsync', '-a', self.settings['portage_confdir'] + '/', dest + '/'],
                    env=self.env)
                self.resume.enable("setup_confdir")

    def portage_overlay(self):
        """ We copy the contents of our overlays to /usr/local/portage """
        if "portage_overlay" in self.settings:
            for x in self.settings["portage_overlay"]:
                if os.path.exists(x):
                    log.info('Copying overlay dir %s', x)
                    ensure_dirs(self.settings['chroot_path'] + self.settings['local_overlay'])
                    cmd("cp -a " + x + "/* " + self.settings["chroot_path"] +
                        self.settings["local_overlay"],
                        env=self.env)

    def root_overlay(self):
        """ Copy over the root_overlay """
        if self.settings["spec_prefix"] + "/root_overlay" in self.settings:
            for x in self.settings[self.settings["spec_prefix"] +
                    "/root_overlay"]:
                if os.path.exists(x):
                    log.info('Copying root_overlay: %s', x)
                    cmd(['rsync', '-a', x + '/', self.settings['chroot_path']],
                        env=self.env)

    def base_dirs(self):
        pass

    def bind(self):
        for x in self.mounts:
            log.debug('bind(); x = %s', x)
            target = support.normpath(self.settings["chroot_path"] + self.target_mounts[x])
            ensure_dirs(target, mode=0o755)

            if not os.path.exists(self.mountmap[x]):
                if self.mountmap[x] not in ("maybe_tmpfs", "tmpfs", "shmfs"):
                    ensure_dirs(self.mountmap[x], mode=0o755)

            src = self.mountmap[x]
            log.debug('bind(); src = %s', src)
            _cmd = None
            if src == "maybe_tmpfs":
                if "var_tmpfs_portage" in self.settings:
                    _cmd = ['mount', '-t', 'tmpfs',
                        '-o', 'size=' + self.settings['var_tmpfs_portage'] + 'G',
                        src, target]
            elif src == "tmpfs":
                _cmd = ['mount', '-t', 'tmpfs', src, target]
            else:
                if os.uname()[0] == "FreeBSD":
                    if src == "/dev":
                        _cmd = ['mount', '-t', 'devfs', 'none', target]
                    else:
                        _cmd = ['mount_nullfs', src, target]
                else:
                    if src == "shmfs":
                        _cmd = ['mount', '-t', 'tmpfs', '-o', 'noexec,nosuid,nodev', 'shm', target]
                    else:
                        _cmd = ['mount', '--bind', src, target]
            if _cmd:
                log.debug('bind(); _cmd = %s', _cmd)
                cmd(_cmd, env=self.env, fail_func=self.unbind)
        log.debug('bind(); finished :D')

    def unbind(self):
        ouch = 0
        mypath = self.settings["chroot_path"]
        myrevmounts = self.mounts[:]
        myrevmounts.reverse()
        # Unmount in reverse order for nested bind-mounts
        for x in myrevmounts:
            target = support.normpath(mypath + self.target_mounts[x])
            if not os.path.exists(target):
                log.notice('%s does not exist. Skipping', target)
                continue

            if not ismount(target):
                log.notice('%s is not a mount point. Skipping', target)
                continue

            try:
                cmd(['umount', target], env=self.env)
            except CatalystError:
                log.warning('First attempt to unmount failed: %s', target)
                log.warning('Killing any pids still running in the chroot')

                self.kill_chroot_pids()

                try:
                    cmd(['umount', target], env=self.env)
                except CatalystError:
                    ouch = 1
                    log.warning("Couldn't umount bind mount: %s", target)
        if ouch:
            # if any bind mounts really failed, then we need to raise
            # this to potentially prevent an upcoming bash stage cleanup script
            # from wiping our bind mounts.
            raise CatalystError("Couldn't umount one or more bind-mounts; aborting for safety.")

    def chroot_setup(self):
        self.makeconf = read_makeconf(support.normpath(self.settings["chroot_path"] + self.settings["make_conf"]))

        if "CBUILD" in self.makeconf:
            self.settings["CBUILD"] = self.makeconf["CBUILD"]
        if "CHOST" in self.makeconf:
            self.settings["CHOST"] = self.makeconf["CHOST"]
        if "CFLAGS" in self.makeconf:
            self.settings["CFLAGS"] = self.makeconf["CFLAGS"]
        if "CXXFLAGS" in self.makeconf:
            self.settings["CXXFLAGS"] = self.makeconf["CXXFLAGS"]
        if "FCFLAGS" in self.makeconf:
            self.settings["FCFLAGS"] = self.makeconf["FCFLAGS"]
        if "FFLAGS" in self.makeconf:
            self.settings["FFLAGS"] = self.makeconf["FFLAGS"]
        if "LDFLAGS" in self.makeconf:
            self.settings["LDFLAGS"] = self.makeconf["LDFLAGS"]
        if "ASFLAGS" in self.makeconf:
            self.settings["ASFLAGS"] = self.makeconf["ASFLAGS"]
        if "COMMON_FLAGS" in self.makeconf:
            self.settings["COMMON_FLAGS"] = self.makeconf["COMMON_FLAGS"]

        if "autoresume" in self.settings["options"] and self.resume.is_enabled("chroot_setup"):
            log.notice('Resume point detected, skipping chroot_setup operation...')
        else:
            log.notice('Setting up chroot...')

            shutil.copy('/etc/resolv.conf', self.settings['chroot_path'] + '/etc/')

            # Copy over the binary interpreter(s) (qemu), if applicable; note that they are given
            # as space-separated list of full paths and go to the same place in the chroot
            if "interpreter" in self.settings:

                if isinstance(self.settings["interpreter"], str):
                    myints = [self.settings["interpreter"]]
                else:
                    myints = self.settings["interpreter"]

                for myi in myints:
                    if not os.path.exists(myi):
                        raise CatalystError(
                            "Can't find interpreter " + myi,
                            print_traceback=True)

                    log.notice('Copying binary interpreter %s into chroot', myi)

                    if os.path.exists(self.settings['chroot_path'] + '/' + myi):
                        os.rename(self.settings['chroot_path'] + '/' + myi, self.settings['chroot_path'] + '/' + myi + '.catalyst')

                    shutil.copy(myi,
                        self.settings['chroot_path'] + '/' + myi)

            # Copy over /etc/hosts from the host in case there are any specialties in there
            hosts_file = self.settings['chroot_path'] + '/etc/hosts'
            if os.path.exists(hosts_file):
                os.rename(hosts_file, hosts_file + '.catalyst')
                shutil.copy('/etc/hosts', hosts_file)
            # write out the make.conf
            try:
                self.write_make_conf(setup=True)
            except OSError as e:
                raise CatalystError('Could not write %s: %s' % (
                    support.normpath(self.settings["chroot_path"] +
                        self.settings["make_conf"]), e))
            self.resume.enable("chroot_setup")

    def write_make_conf(self, setup=True):
        # Modify and write out make.conf (for the chroot)
        makepath = support.normpath(self.settings["chroot_path"] + self.settings["make_conf"])
        clear_path(makepath)
        with open(makepath, "w") as myf:
            log.notice("Writing the stage make.conf to: %s" % makepath)
            myf.write("# These settings were set by the catalyst build script "
                    "that automatically\n# built this stage.\n")
            myf.write("# Please consult "
                    "/usr/share/portage/config/make.conf.example "
                    "for a more\n# detailed example.\n")

            for flags in ["COMMON_FLAGS", "CFLAGS", "CXXFLAGS", "FCFLAGS", "FFLAGS",
                        "LDFLAGS", "ASFLAGS"]:
                if flags in ["LDFLAGS", "ASFLAGS"]:
                    if not flags in self.settings:
                        continue
                    myf.write("# %s is unsupported.  USE AT YOUR OWN RISK!\n"
                            % flags)
                if flags not in self.settings or (flags is not "COMMON_FLAGS" and
                        self.settings[flags] == self.settings["COMMON_FLAGS"]):
                    myf.write('%s="${COMMON_FLAGS}"\n' % flags)
                elif isinstance(self.settings[flags], list):
                    myf.write('%s="%s"\n'
                            % (flags, ' '.join(self.settings[flags])))
                else:
                    myf.write('%s="%s"\n'
                            % (flags, self.settings[flags]))

            if "CBUILD" in self.settings:
                myf.write("# This should not be changed unless you know exactly"
                    " what you are doing.  You\n# should probably be "
                    "using a different stage, instead.\n")
                myf.write('CBUILD="' + self.settings["CBUILD"] + '"\n')

            if "CHOST" in self.settings:
                myf.write("# WARNING: Changing your CHOST is not something "
                    "that should be done lightly.\n# Please consult "
                    "https://wiki.gentoo.org/wiki/Changing_the_CHOST_variable "
                    "before changing.\n")
                myf.write('CHOST="' + self.settings["CHOST"] + '"\n')

            # Figure out what our USE vars are for building
            myusevars = []
            if "bindist" in self.settings["options"]:
                myf.write("\n# NOTE: This stage was built with the bindist Use flag enabled\n")
            if setup or "sticky-config" in self.settings["options"]:
                myusevars.extend(self.settings["catalyst_use"])
                log.notice("STICKY-CONFIG is enabled")
            if "HOSTUSE" in self.settings:
                myusevars.extend(self.settings["HOSTUSE"])

            if "use" in self.settings:
                myusevars.extend(self.settings["use"])

            if myusevars:
                myf.write("# These are the USE and USE_EXPAND flags that were "
                        "used for\n# building in addition to what is provided "
                        "by the profile.\n")
                myusevars = sorted(set(myusevars))
                myf.write('USE="' + ' '.join(myusevars) + '"\n')
                if '-*' in myusevars:
                    log.warning(
                        'The use of -* in %s/use will cause portage to ignore\n'
                        'package.use in the profile and portage_confdir.\n'
                        "You've been warned!", self.settings['spec_prefix'])

            myuseexpandvars = {}
            if "HOSTUSEEXPAND" in self.settings:
                for hostuseexpand in self.settings["HOSTUSEEXPAND"]:
                    myuseexpandvars.update(
                        {hostuseexpand:self.settings["HOSTUSEEXPAND"][hostuseexpand]})

            if myuseexpandvars:
                for hostuseexpand in myuseexpandvars:
                    myf.write(hostuseexpand + '="' +
                        ' '.join(myuseexpandvars[hostuseexpand]) + '"\n')
            # write out a shipable version
            target_portdir = support.normpath(self.settings["repo_basedir"] + "/" +
                self.settings["repo_name"])

            myf.write('PORTDIR="%s"\n' % target_portdir)
            myf.write('DISTDIR="%s"\n' % self.settings['target_distdir'])
            myf.write('PKGDIR="%s"\n' % self.settings['target_pkgdir'])
            if setup:
                # Setup the portage overlay
                if "portage_overlay" in self.settings:
                    myf.write('PORTDIR_OVERLAY="%s"\n' %  self.settings["local_overlay"])

            # Set default locale for system responses. #478382
            myf.write(
                '\n'
                '# This sets the language of build output to English.\n'
                '# Please keep this setting intact when reporting bugs.\n'
                'LC_MESSAGES=C\n')

    def rcupdate(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("rcupdate"):
            log.notice('Resume point detected, skipping rcupdate operation...')
        else:
            if os.path.exists(self.settings["controller_file"]):
                cmd([self.settings['controller_file'], 'rc-update'],
                    env=self.env)
                self.resume.enable("rcupdate")

    def clean(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("clean"):
            log.notice('Resume point detected, skipping clean operation...')
        else:
            for x in self.settings["cleanables"]:
                log.notice('Cleaning chroot: %s', x)
                clear_path(support.normpath(self.settings["destpath"] + x))

        # Put /etc/hosts back into place
        hosts_file = self.settings['chroot_path'] + '/etc/hosts'
        if os.path.exists(hosts_file + '.catalyst'):
            os.rename(hosts_file + '.catalyst', hosts_file)

        # optionally clean up binary interpreter(s)
        if "interpreter" in self.settings:

            if isinstance(self.settings["interpreter"], str):
                myints = [self.settings["interpreter"]]
            else:
                myints = self.settings["interpreter"]

            for myi in myints:
                if os.path.exists(self.settings['chroot_path'] + '/' + myi + '.catalyst'):
                    os.rename(self.settings['chroot_path'] + '/' + myi + '.catalyst', self.settings['chroot_path'] + '/' + myi)
                else:
                    os.remove(self.settings['chroot_path'] + '/' + myi)

        # optionally clean up portage configs
        if ("portage_prefix" in self.settings and
            "sticky-config" not in self.settings["options"]):
            log.debug("clean(), portage_preix = %s, no sticky-config", self.settings["portage_prefix"])
            for _dir in "package.accept_keywords", "package.keywords", "package.mask", "package.unmask", "package.use", "package.env", "env":
                target = os.path.join(self.settings["destpath"],
                    "etc/portage/%s" % _dir,
                    self.settings["portage_prefix"])
                log.notice("Clearing portage_prefix target: %s", target)
                clear_path(target)

        # Remove hacks that should *never* go into stages
        target = os.path.join(self.settings["destpath"], "etc/portage/patches")
        if os.path.exists(target):
            log.warning("You've been hacking. Clearing target patches: %s", target)
            clear_path(target)

        # Remove our overlay
        overlay = support.normpath(self.settings["chroot_path"] + self.settings["local_overlay"])
        if os.path.exists(overlay):
            clear_path(overlay)

        if "sticky-config" not in self.settings["options"]:
            # re-write the make.conf to be sure it is clean
            self.write_make_conf(setup=False)

        # Clean up old and obsoleted files in /etc
        if os.path.exists(self.settings["stage_path"]+"/etc"):
            cmd(['find', self.settings['stage_path'] + '/etc',
                '-maxdepth', '1', '-name', '*-', '-delete'],
                env=self.env)

        if os.path.exists(self.settings["controller_file"]):
            cmd([self.settings['controller_file'], 'clean'], env=self.env)
            self.resume.enable("clean")

    def empty(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("empty"):
            log.notice('Resume point detected, skipping empty operation...')
        else:
            if self.settings["spec_prefix"] + "/empty" in self.settings:
                if isinstance(
                        self.settings[self.settings['spec_prefix'] + '/empty'],
                        str):
                    self.settings[self.settings["spec_prefix"] + "/empty"] = \
                        self.settings[self.settings["spec_prefix"] + \
                        "/empty"].split()
                for x in self.settings[self.settings["spec_prefix"] + "/empty"]:
                    myemp = self.settings["destpath"] + x
                    if not os.path.isdir(myemp) or os.path.islink(myemp):
                        log.warning('not a directory or does not exist, '
                            'skipping "empty" operation: %s', x)
                        continue
                    log.info('Emptying directory %s', x)
                    clear_dir(myemp)
            self.resume.enable("empty")

    def remove(self):
        if self.settings["spec_prefix"] + "/rm" in self.settings:
            for x in self.settings[self.settings["spec_prefix"] + "/rm"]:
                # We're going to shell out for all these cleaning
                # operations, so we get easy glob handling.
                log.notice('livecd: removing %s', x)
                clear_path(self.settings["chroot_path"] + x)
            try:
                if os.path.exists(self.settings["controller_file"]):
                    cmd([self.settings['controller_file'], 'clean'], env=self.env)
                    self.resume.enable("remove")
            except:
                self.unbind()
                raise

    def preclean(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("preclean"):
            log.notice('Resume point detected, skipping preclean operation...')
        else:
            try:
                if os.path.exists(self.settings["controller_file"]):
                    cmd([self.settings['controller_file'], 'preclean'],
                        env=self.env)
                    self.resume.enable("preclean")

            except:
                self.unbind()
                raise CatalystError("Build failed, could not execute preclean")

    def capture(self):
        # initialize it here so it doesn't use
        # resources if it is not needed
        if not self.compressor:
            self.compressor = CompressMap(self.settings["compress_definitions"],
                env=self.env, default_mode=self.settings['compression_mode'],
                comp_prog=self.settings['comp_prog'])

        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("capture"):
            log.notice('Resume point detected, skipping capture operation...')
        else:
            log.notice('Capture target in a tarball')
            # Remove filename from path
            mypath = os.path.dirname(self.settings["target_path"].rstrip('/'))

            # Now make sure path exists
            ensure_dirs(mypath)

            pack_info = self.compressor.create_infodict(
                source=".",
                basedir=self.settings["stage_path"],
                filename=self.settings["target_path"].rstrip('/'),
                mode=self.settings["compression_mode"],
                auto_extension=True,
                arch=self.settings["compressor_arch"],
                other_options=self.settings["compressor_options"],
                )
            target_filename = ".".join([self.settings["target_path"].rstrip('/'),
                self.compressor.extension(pack_info['mode'])])

            log.notice('Creating stage tarball... mode: %s',
                self.settings['compression_mode'])

            if self.compressor.compress(pack_info):
                self.gen_contents_file(target_filename)
                self.gen_digest_file(target_filename)
                self.resume.enable("capture")
            else:
                log.warning("Couldn't create stage tarball: %s", target_filename)

    def run_local(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("run_local"):
            log.notice('Resume point detected, skipping run_local operation...')
        else:
            try:
                if os.path.exists(self.settings["controller_file"]):
                    log.info('run_local() starting controller script...')
                    cmd([self.settings['controller_file'], 'run'],
                        env=self.env)
                    self.resume.enable("run_local")
                else:
                    log.info('run_local() no controller_file found... %s',
                        self.settings['controller_file'])

            except CatalystError:
                self.unbind()
                raise CatalystError("Stage build aborting due to error.",
                    print_traceback=False)

    def setup_environment(self):
        """
        Modify the current environment. This is an ugly hack that should be
        fixed. We need this to use the os.system() call since we can't
        specify our own environ
        """
        log.debug('setup_environment(); settings = %r', self.settings)
        for x in list(self.settings):
            log.debug('setup_environment(); processing: %s', x)
            if x == "options":
                #self.env['clst_' + x] = ' '.join(self.settings[x])
                for opt in self.settings[x]:
                    self.env['clst_' + opt.upper()] = "true"
                continue
            # Sanitize var names by doing "s|/-.|_|g"
            varname = "clst_" + x.replace("/", "_")
            varname = varname.replace("-", "_")
            varname = varname.replace(".", "_")
            if isinstance(self.settings[x], str):
                # Prefix to prevent namespace clashes
                #os.environ[varname] = self.settings[x]
                if "path" in x:
                    self.env[varname] = self.settings[x].rstrip("/")
                else:
                    self.env[varname] = self.settings[x]
            elif isinstance(self.settings[x], list):
                #os.environ[varname] = ' '.join(self.settings[x])
                self.env[varname] = ' '.join(self.settings[x])
            elif isinstance(self.settings[x], bool):
                if self.settings[x]:
                    self.env[varname] = "true"
                else:
                    self.env[varname] = "false"
            # This handles a dictionary of objects just one level deep and no deeper!
            # Its currently used only for USE_EXPAND flags which are dictionaries of
            # lists in arch/amd64.py and friends.  If we wanted self.settigs[var]
            # of any depth, we should make this function recursive.
            elif isinstance(self.settings[x], dict):
                if x in ["compress_definitions",
                    "decompress_definitions"]:
                    continue
                self.env[varname] = ' '.join(self.settings[x].keys())
                for y in self.settings[x].keys():
                    varname2 = "clst_" + y.replace("/", "_")
                    varname2 = varname2.replace("-", "_")
                    varname2 = varname2.replace(".", "_")
                    if isinstance(self.settings[x][y], str):
                        self.env[varname2] = self.settings[x][y]
                    elif isinstance(self.settings[x][y], list):
                        self.env[varname2] = ' '.join(self.settings[x][y])
                    elif isinstance(self.settings[x][y], bool):
                        if self.settings[x][y]:
                            self.env[varname] = "true"
                        else:
                            self.env[varname] = "false"

        if "makeopts" in self.settings:
            if isinstance(self.settings["makeopts"], str):
                self.env["MAKEOPTS"] = self.settings["makeopts"]
            else:
                # ensure makeopts is a string
                self.env["MAKEOPTS"] = ' '.join(self.settings["makeopts"])

        log.debug('setup_environment(); env = %r', self.env)

    def run(self):
        with fasteners.InterProcessLock(self.settings["chroot_path"] + '.lock'):
            return self._run()

    def _run(self):
        # Kill any pids in the chroot
        self.kill_chroot_pids()

        # Check for mounts right away and abort if we cannot unmount them
        self.mount_safety_check()

        if "clear-autoresume" in self.settings["options"]:
            self.clear_autoresume()

        if "purgetmponly" in self.settings["options"]:
            self.purge()
            return

        if "purgeonly" in self.settings["options"]:
            log.info('StageBase: run() purgeonly')
            self.purge()

        if "purge" in self.settings["options"]:
            log.info('StageBase: run() purge')
            self.purge()

        failure = False
        for x in self.settings["action_sequence"]:
            log.notice('--- Running action sequence: %s', x)
            sys.stdout.flush()
            try:
                getattr(self, x)()
            except Exception:
                log.error('Exception running action sequence %s', x, exc_info=True)
                failure = True
                break

        if failure:
            log.notice('Cleaning up... Running unbind()')
            self.unbind()
            return False
        return True


    def unmerge(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("unmerge"):
            log.notice('Resume point detected, skipping unmerge operation...')
        else:
            if self.settings["spec_prefix"] + "/unmerge" in self.settings:
                if isinstance(self.settings[self.settings['spec_prefix'] + '/unmerge'], str):
                    self.settings[self.settings["spec_prefix"] + "/unmerge"] = \
                        [self.settings[self.settings["spec_prefix"] + "/unmerge"]]

                # Before cleaning, unmerge stuff
                try:
                    cmd([self.settings['controller_file'], 'unmerge'] +
                        self.settings[self.settings['spec_prefix'] + '/unmerge'],
                        env=self.env)
                    log.info('unmerge shell script')
                except CatalystError:
                    self.unbind()
                    raise
                self.resume.enable("unmerge")

    def target_setup(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("target_setup"):
            log.notice('Resume point detected, skipping target_setup operation...')
        else:
            log.notice('Setting up filesystems per filesystem type')
            cmd([self.settings['controller_file'], 'target_image_setup',
                self.settings['target_path']], env=self.env)
            self.resume.enable("target_setup")

    def setup_overlay(self):
        if "autoresume" in self.settings["options"] \
        and self.resume.is_enabled("setup_overlay"):
            log.notice('Resume point detected, skipping setup_overlay operation...')
        else:
            if self.settings["spec_prefix"] + "/overlay" in self.settings:
                for x in self.settings[self.settings["spec_prefix"] + "/overlay"]:
                    if os.path.exists(x):
                        cmd(['rsync', '-a', x + '/', self.settings['target_path']],
                            env=self.env)
                self.resume.enable("setup_overlay")

    def build_packages(self):
        build_packages_resume = os.path.join(self.settings["autoresume_path"],
            "build_packages")
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("build_packages"):
            log.notice('Resume point detected, skipping build_packages operation...')
        else:
            if self.settings["spec_prefix"] + "/packages" in self.settings:
                target_pkgs = self.settings["spec_prefix"] + '/packages'
                if "autoresume" in self.settings["options"] \
                    and self.resume.is_enabled("build_packages"):
                    log.notice('Resume point detected, skipping build_packages '
                        'operation...')
                else:
                    command = [self.settings['controller_file'], 'build_packages']
                    if isinstance(self.settings[target_pkgs], str):
                        command.append(self.settings[target_pkgs])
                    else:
                        command.extend(self.settings[target_pkgs])
                    try:
                        cmd(command, env=self.env)
                        fileutils.touch(build_packages_resume)
                        self.resume.enable("build_packages")
                    except CatalystError:
                        self.unbind()
                        raise CatalystError(
                            self.settings["spec_prefix"] +
                            "build aborting due to error.")

    def build_kernel(self):
        '''Build all configured kernels'''
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("build_kernel"):
            log.notice('Resume point detected, skipping build_kernel operation...')
        else:
            if "boot/kernel" in self.settings:
                try:
                    mynames = self.settings["boot/kernel"]
                    if isinstance(mynames, str):
                        mynames = [mynames]
                    # Execute the script that sets up the kernel build environment
                    cmd([self.settings['controller_file'], 'pre-kmerge'],
                        env=self.env)
                    for kname in mynames:
                        self._build_kernel(kname = kname)
                    self.resume.enable("build_kernel")
                except CatalystError:
                    self.unbind()
                    raise CatalystError(
                        "build aborting due to kernel build error.",
                        print_traceback=True)

    def _build_kernel(self, kname):
        "Build a single configured kernel by name"
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("build_kernel_" + kname):
            log.notice('Resume point detected, skipping build_kernel '
                'for %s operation...', kname)
            return
        self._copy_kernel_config(kname=kname)

        # If we need to pass special options to the bootloader
        # for this kernel put them into the environment
        key = 'boot/kernel/' + kname + '/kernelopts'
        if key in self.settings:
            myopts = self.settings[key]

            if not isinstance(myopts, str):
                myopts = ' '.join(myopts)
                self.env[kname + "_kernelopts"] = myopts
            else:
                self.env[kname + "_kernelopts"] = ""

        key = 'boot/kernel/' + kname + '/extraversion'
        self.settings.setdefault(key, '')
        self.env["clst_kextraversion"] = self.settings[key]

        self._copy_initramfs_overlay(kname=kname)

        # Execute the script that builds the kernel
        cmd([self.settings['controller_file'], 'kernel', kname],
            env=self.env)

        if "boot/kernel/" + kname + "/initramfs_overlay" in self.settings:
            log.notice('Cleaning up temporary overlay dir')
            clear_dir(self.settings['chroot_path'] + '/tmp/initramfs_overlay/')

        self.resume.is_enabled("build_kernel_" + kname)

        # Execute the script that cleans up the kernel build environment
        cmd([self.settings['controller_file'], 'post-kmerge'],
            env=self.env)

    def _copy_kernel_config(self, kname):
        key = 'boot/kernel/' + kname + '/config'
        if key in self.settings:
            if not os.path.exists(self.settings[key]):
                self.unbind()
                raise CatalystError("Can't find kernel config: %s" %
                    self.settings[key])

            try:
                shutil.copy(self.settings[key],
                    self.settings['chroot_path'] + '/var/tmp/' + kname + '.config')

            except IOError:
                self.unbind()

    def _copy_initramfs_overlay(self, kname):
        key = 'boot/kernel/' + kname + '/initramfs_overlay'
        if key in self.settings:
            if os.path.exists(self.settings[key]):
                log.notice('Copying initramfs_overlay dir %s', self.settings[key])

                ensure_dirs(
                    self.settings['chroot_path'] +
                    '/tmp/initramfs_overlay/' + self.settings[key])

                cmd('cp -R ' + self.settings[key] + '/* ' +
                    self.settings['chroot_path'] +
                    '/tmp/initramfs_overlay/' + self.settings[key], env=self.env)

    def bootloader(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("bootloader"):
            log.notice('Resume point detected, skipping bootloader operation...')
        else:
            try:
                cmd([self.settings['controller_file'], 'bootloader',
                    self.settings['target_path'].rstrip('/')],
                    env=self.env)
                self.resume.enable("bootloader")
            except CatalystError:
                self.unbind()
                raise CatalystError("Script aborting due to error.")

    def livecd_update(self):
        if "autoresume" in self.settings["options"] \
            and self.resume.is_enabled("livecd_update"):
            log.notice('Resume point detected, skipping build_packages operation...')
        else:
            try:
                cmd([self.settings['controller_file'], 'livecd-update'],
                    env=self.env)
                self.resume.enable("livecd_update")

            except CatalystError:
                self.unbind()
                raise CatalystError("build aborting due to livecd_update error.")

    @staticmethod
    def _debug_pause_():
        input("press any key to continue: ")

    def gen_contents_file(self, path):
        contents = path + ".CONTENTS.gz"
        if os.path.exists(contents):
            os.remove(contents)
        if "contents" in self.settings:
            contents_map = self.settings["contents_map"]
            if os.path.exists(path):
                with gzip.open(contents, "wt", encoding='utf-8') as myf:
                    keys={}
                    for i in self.settings["contents"].split():
                        keys[i]=1
                        array = sorted(keys.keys())
                    for j in array:
                        contents = contents_map.contents(path, j,
                            verbose=self.settings["VERBOSE"])
                        if contents:
                            myf.write(contents)

    def gen_digest_file(self, path):
        digests = path + ".DIGESTS"
        if os.path.exists(digests):
            os.remove(digests)
        if "digests" in self.settings:
            hash_map = self.settings["hash_map"]
            if os.path.exists(path):
                with io.open(digests, "w", encoding='utf-8') as myf:
                    keys={}
                    for i in self.settings["digests"].split():
                        keys[i]=1
                        array = sorted(keys.keys())
                    for f in [path, path + '.CONTENTS.gz']:
                        if os.path.exists(f):
                            if "all" in array:
                                for k in list(hash_map.hash_map):
                                    digest = hash_map.generate_hash(f, hash_=k)
                                    myf.write(digest)
                            else:
                                for j in array:
                                    digest = hash_map.generate_hash(f, hash_=j)
                                    myf.write(digest)

    def clear_autoresume(self):
        """ Clean resume points since they are no longer needed """
        if "autoresume" in self.settings["options"]:
            log.notice('Removing AutoResume Points ...')
            self.resume.clear_all()


    def remove_autoresume(self):
        """ Rmove all resume points since they are no longer needed """
        if "autoresume" in self.settings["options"]:
            log.notice('Removing AutoResume ...')
            self.resume.clear_all(remove=True)


    def clear_chroot(self):
        log.notice('Clearing the chroot path ...')
        clear_dir(self.settings["chroot_path"], mode=0o755, chg_flags=True)


    def remove_chroot(self):
        log.notice('Removing the chroot path ...')
        clear_dir(self.settings["chroot_path"], mode=0o755, chg_flags=True, remove=True)


    def clear_packages(self, remove=False):
        if "pkgcache" in self.settings["options"]:
            log.notice('purging the pkgcache ...')
            clear_dir(self.settings["pkgcache_path"], remove=remove)


    def clear_kerncache(self, remove=False):
        if "kerncache" in self.settings["options"]:
            log.notice('purging the kerncache ...')
            clear_dir(self.settings["kerncache_path"], remove=remove)


    def purge(self, remove=False):
        countdown(10,"Purging Caches ...")
        if any(k in self.settings["options"] for k in ("purge",
                "purgeonly", "purgetmponly")):
            log.notice('purge(); clearing autoresume ...')
            self.clear_autoresume()

            log.notice('purge(); clearing chroot ...')
            self.clear_chroot()

            if "purgetmponly" not in self.settings["options"]:
                log.notice('purge(); clearing package cache ...')
                self.clear_packages(remove)

            log.notice('purge(); clearing kerncache ...')
            self.clear_kerncache(remove)

    def _addl_arg_parse(self, myspec):
        "helper function to help targets parse additional arguments"
        messages = []

        for x in myspec:
            if x not in self.valid_values and x not in self.required_values:
                messages.append(f"Argument \"{x}\" not recognized.")

        for x in self.required_values:
            if x not in myspec:
                messages.append(f"Required argument \"{x}\" not specified.")

        if len(messages) > 0:
            raise CatalystError('\n\tAlso: '.join(messages))







class generic:

    def __init__(self, myspec):
        self.settings = myspec
        self.settings.setdefault('CHROOT', 'chroot')

    def setarch(self, arch):
        """Set the chroot wrapper to run through `setarch |arch|`

        Useful for building x86-on-amd64 and such.
        """
        if os.uname()[0] == 'Linux':
            self.settings['CHROOT'] = 'setarch %s %s' % (arch, self.settings['CHROOT'])

    def mount_safety_check(self):
        """
        Make sure that no bind mounts exist in chrootdir (to use before
        cleaning the directory, to make sure we don't wipe the contents of
        a bind mount
        """
        pass

    def mount_all(self):
        """do all bind mounts"""
        pass

    def umount_all(self):
        """unmount all bind mounts"""
        pass
