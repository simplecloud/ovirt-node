#!/usr/bin/python
# ovirtfunctions.py - Copyright (C) 2010 Red Hat, Inc.
# Written by Joey Boggs <jboggs@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.  A copy of the GNU General Public License is
# also available at http://www.gnu.org/copyleft/gpl.html.

import subprocess
from subprocess import Popen, PIPE, STDOUT
from xml.dom.minidom import parseString
import os
import tempfile
import string
import sys
import augeas
import socket
import fcntl
import struct
import hashlib
import shutil
import re
import gudev
import cracklib
import libvirt
import logging

OVIRT_LOGFILE="/var/log/ovirt.log"
OVIRT_TMP_LOGFILE="/tmp/ovirt.log"

# label of the oVirt partition
OVIRT_LABEL="OVIRT"
# configuration values are loaded in the following order:
# 1. /etc/sysconfig/node-config sets the default values
# 2. /etc/default/ovirt is loaded to override defaults with karg values
NODE_SYSCONFIG="/etc/sysconfig/node-config"
OVIRT_DEFAULTS="/etc/default/ovirt"
aug = augeas.Augeas()
#   workaround for bind-mounted files
#   see https://fedorahosted.org/augeas/ticket/32
aug.set("/augeas/save/copy_if_rename_fails", "")

# read product / version info
PRODUCT_SHORT = aug.get("/files/etc/default/version/PRODUCT_SHORT")
if PRODUCT_SHORT == None:
    PRODUCT_SHORT = "oVirt"
else:
    PRODUCT_SHORT = PRODUCT_SHORT.strip("'\"")
PRODUCT_VERSION = aug.get("/files/etc/default/version/VERSION")
PRODUCT_RELEASE = aug.get("/files/etc/default/version/RELEASE")

OVIRT_VARS = {}
# Parse all OVIRT_* variables

def parse_defaults():
    global OVIRT_VARS
    if os.path.exists(NODE_SYSCONFIG):
        try:
            f = open(NODE_SYSCONFIG, 'r')
            for line in f:
                try:
                    line = line.strip()
                    key, value = line.split("\"", 1)
                    key = key.strip("=")
                    key = key.strip()
                    value = value.strip("\"")
                    OVIRT_VARS[key] = value
                except:
                    pass
            f.close()
        except:
            pass

    f = open(OVIRT_DEFAULTS, 'r')
    for line in f:
        try:
            line = line.strip()
            key, value = line.split("=", 1)
            key = key.strip("=")
            value = value.strip("\"")
            OVIRT_VARS[key] = value
        except:
            pass
    f.close()
    return OVIRT_VARS


# fallback when default is empty
#OVIRT_STANDALONE=${OVIRT_STANDALONE:-0}

OVIRT_BACKUP_DIR="/var/lib/ovirt-backup"

MANAGEMENT_SCRIPTS_DIR="/etc/node.d"

def log(log_entry):
    if is_stateless():
        log_file = open(OVIRT_LOGFILE, "a")
    elif is_firstboot():
        log_file = open(OVIRT_TMP_LOGFILE, "a")
    else:
        log_file = open(OVIRT_LOGFILE, "a")
    try:
        log_file.write(log_entry +"\n")
    except:
        log_file.write(str(log_entry))
    log_file.close()

def augtool(oper, key, value):
    if oper == "set":
        aug.set(key, value)
        aug.save()
        return
    elif oper == "rm":
        aug.remove(key)
        aug.save()
        return
    elif oper == "get":
        value = aug.get(key)
        return value
    elif oper == "match":
        value = aug.match(key)
        return value

def augtool_get(key):
    value = aug.get(key)
    return value

def subprocess_closefds(*args, **kwargs):
    kwargs.update({
        "close_fds": True
    })
    #logger.debug("Running in subprocess: %s" % ((args, kwargs),))
    return subprocess.Popen(*args, **kwargs)

def system_closefds(cmd):
    proc = subprocess_closefds(cmd, shell=True)
    return proc.wait()

class passthrough(object):
    proc = None
    retval = None
    stdout = None

    def __init__(self, cmd, log_func=None):
        import subprocess as sp
        if log_func is not None:
            log_func("Running: %s" % cmd)
        self.proc = sp.Popen(cmd, shell=True, stdout=sp.PIPE, \
                             stderr=sp.STDOUT)
        self.stdout = self.proc.stdout.read()
        self.retval = self.proc.wait()

    def __str__(self):
        return self.stdout

# return 1 if oVirt Node is running in standalone mode
# return 0 if oVirt Node is managed by the oVirt Server
def is_managed():
    if "ovirt_standalone" in OVIRT_VARS["OVIRT_BOOTPARAMS"]:
        return False
    else:
        return True

# oVirt Node in standalone mode does not try to contact the oVirt Server
def is_standalone(self):
    if is_managed:
        return False
    else:
        return True

# return 0 if local storage is configured
# return 1 if local storage is not configured
def is_local_storage_configured():
    ret = system_closefds("lvs HostVG/Config &>/dev/null")
    if ret > 0:
        return False
    return True

# perform automatic local disk installation
# when at least following boot parameters are present:
# for networking - OVIRT_BOOTIF, management NIC
#       if other ip bootparams are not specified, IPv4 DHCP is assumed
# for storage - OVIRT_INIT, local disk to use
#       if ovirt_vol is not specified, default volume sizes are set
def is_auto_install():
    if OVIRT_VARS.has_key("OVIRT_BOOTIF") and OVIRT_VARS.has_key("OVIRT_INIT"):
        return True
    else:
        return False

# return 0 if this is an upgrade
# return 1 otherwise
def is_upgrade(self):
    if self.OVIRT_VARS.has_key("OVIRT_UPGRADE") and self.OVIRT_VARS["OVIRT_UPGRADE"] == 1:
        return True
    else:
        return False

# return 0 if booted from local disk
# return 1 if booted from other media
def is_booted_from_local_disk():
    ret = system_closefds("grep -q LABEL=Root /proc/cmdline")
    if ret == 0:
        return True
    else:
        return False

def is_rescue_mode():
    ret = system_closefds("grep -q rescue /proc/cmdline")
    if ret == 0:
        return True
    # check for runlevel 1/single
    else:
        ret = system_closefds("runlevel|grep -q '1\|S'")
        if ret == 0:
            return True
        return False

def get_ttyname():
    for f in sys.stdin, sys.stdout, sys.stderr:
        if f.isatty():
            tty = os.ttyname(f.fileno()).replace("/dev/","")
            if "pts" in tty:
                tty = tty.replace("/","")
            return tty
    return None

def is_console():
    # /dev/console only used during install
    tty = get_ttyname()
    if "console" in tty:
        return True
    # serial console
    elif "ttyS" in tty:
        return False
    # local console
    elif "tty" in tty:
        return True
    else:
        return False

def manual_setup():
    logger.info("Checking For Setup Lockfile")
    tty = get_ttyname()
    if os.path.exists("/tmp/ovirt-setup.%s" % tty):
        return True
    else:
        return False

def manual_teardown():
    logger.info("Removing Setup Lockfile")
    tty = get_ttyname()
    os.unlink("/tmp/ovirt-setup.%s" % tty)

# was firstboot menu already shown?
# state is stored in persistent config partition
def is_firstboot():
    # check if theres a key first
    if OVIRT_VARS.has_key("OVIRT_FIRSTBOOT"):
        if OVIRT_VARS["OVIRT_FIRSTBOOT"] == "1":
            return True
        elif OVIRT_VARS["OVIRT_FIRSTBOOT"] == "0":
            return False
    # in case there's no key, default to True unless booted from disk
    if is_booted_from_local_disk():
        return False
    else:
        return True

def is_cim_enabled():
    # check if theres a key first
    # reload OVIRT_VARS
    OVIRT_VARS = parse_defaults()
    if OVIRT_VARS.has_key("OVIRT_CIM_ENABLED"):
        if OVIRT_VARS["OVIRT_CIM_ENABLED"] == "1":
            return True
        elif OVIRT_VARS["OVIRT_CIM_ENABLED"] == "0":
            return False
    return False

def is_stateless():
    # check if theres a key first
    if OVIRT_VARS.has_key("OVIRT_STATELESS"):
        if OVIRT_VARS["OVIRT_STATELESS"] == "1":
            return True
        elif OVIRT_VARS["OVIRT_STATELESS"] == "0":
            return False
    return False

def disable_firstboot():
    if os.path.ismount("/config"):
        aug.set("/files/etc/default/ovirt/OVIRT_FIRSTBOOT", "0")
        aug.set("/files/etc/default/ovirt/OVIRT_INIT", '""')
        aug.set("/files/etc/default/ovirt/OVIRT_UPGRADE", "0")
        aug.save()
        ovirt_store_config("/etc/default/ovirt")

# Destroys a particular volume group and its logical volumes.
# The input (vg) is accepted as either the vg_name or vg_uuid
def wipe_volume_group(vg):
    vg_name_cmd = "vgs -o vg_name,vg_uuid --noheadings 2>/dev/null | grep -w \"" + vg + "\" | awk '{print $1}'"
    vg_name = subprocess_closefds(vg_name_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    vg = vg_name.stdout.read().strip()
    files_cmd = "grep '%s' /proc/mounts|awk '{print $2}'|sort -r" % vg
    files = subprocess_closefds(files_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    files_output = files.stdout.read()
    logger.debug("Mounts:\n" + files_output)
    for file in files_output.split():
        system_closefds("umount %s &>/dev/null" % file)
    swap_cmd = "grep '%s' /proc/swaps|awk '{print $1}'" % vg
    swap = subprocess_closefds(swap_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    swap_output = swap.stdout.read().strip()
    for d in swap_output.split():
        system_closefds("swapoff %s &>/dev/null" % d)
    # Deactivate VG
    passthrough("vgchange -a n -v %s" % vg)
    ret = -1
    i = 1
    vgremove_cmd = "vgremove -f -v %s" % vg
    # Try to remove VG for i-times
    while ret is not 0 and i > 0:
        logger.debug("Removing VG '%s' (Try #%d)" % (vg, i))
        vgremove_proc = passthrough(vgremove_cmd, logger.debug)
        ret = vgremove_proc.retval
        i -= 1

# find_srv SERVICE PROTO
#
# reads DNS SRV record
# sets SRV_HOST and SRV_PORT if DNS SRV record found, clears them if not
# Example usage:
# find_srv ovirt tcp
def find_srv(srv, proto):
    domain = subprocess_closefds("dnsdomainname 2>/dev/null", shell=True, stdout=PIPE, stderr=STDOUT)
    domain_output = domain.stdout.read()
    if domain_output == "localdomain":
        domain=""
    # FIXME dig +search does not seem to work with -t srv
    # dnsreply=$(dig +short +search -t srv _$1._$2)
    # This is workaround:
    search = subprocess_closefds("grep search /etc/resolv.conf", shell=True, stdout=PIPE, stderr=STDOUT)
    search_output = search.stdout.read()
    search = search_output.replace("search ","")
    domain_search = domain_output + search_output
    for d in domain_search.split():
        dig_cmd = "dig +short -t srv _%s._%s.%s" % (srv, proto,search)
        dig = subprocess_closefds(dig_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
        dig_output = dig.stdout.read()
        dig.poll()
        dig_rc = dig.returncode
        if dig_rc == 0:
            try:
                a,b,port,host = dig_output.split("=", 4)
                return (port, host)
            except:
                logger.error("Unable to find srv records")
        return False

def ovirt_setup_libvirtd(self):
    # just to get a boot warning to shut up
    system_closefds("touch /etc/resolv.conf")

    # make libvirtd listen on the external interfaces
    system_closefds("sed -i -e 's/^#\(LIBVIRTD_ARGS=\"--listen\"\).*/\1/' /etc/sysconfig/libvirtd")

    # set up qemu daemon to allow outside VNC connections
    system_closefds("sed -i -e 's/^[[:space:]]*#[[:space:]]*\(vnc_listen = \"0.0.0.0\"\).*/\1/' /etc/libvirt/qemu.conf")
    # set up libvirtd to listen on TCP (for kerberos)
    system_closefds('sed -i -e "s/^[[:space:]]*#[[:space:]]*\(listen_tcp\)\>.*/\1 = 1/" \
       -e "s/^[[:space:]]*#[[:space:]]*\(listen_tls\)\>.*/\1 = 0/" \
       /etc/libvirt/libvirtd.conf')

def ovirt_setup_anyterm():
    # configure anyterm
    anyterm_conf = open("/etc/sysconfig/anyterm", "w")
    anyterm_conf.write("ANYTERM_CMD='sudo /usr/bin/virsh console %p'")
    anyterm_conf.write("ANYTERM_LOCAL_ONLY=false")
    anyterm_conf.close()
    # permit it to run the virsh console
    system_closefds("echo 'anyterm ALL=NOPASSWD: /usr/bin/virsh console *' >> /etc/sudoers")

# mount livecd media
# e.g. CD /dev/sr0, USB /dev/sda1,
# PXE /dev/loop0 (loopback ISO)
# not available when booted from local disk installation
def mount_live():
    live_dev = ""
    if os.path.ismount("/live"):
        if os.path.exists("/live/isolinux") or os.path.exists("/live/syslinux"):
            return True
    if not os.path.exists("/dev/live"):
        if system("losetup /dev/loop0|grep -q '\.iso'"):
            # PXE boot
            live_dev="/dev/loop0"
        else:
            try:
                # /dev/live if not exist alternative
                client = gudev.Client(['block'])
                cmdline = open("/proc/cmdline")
                cdlabel = re.search('CDLABEL\=([a-zA-Z0-9_\.-]+)', cmdline.read())
                cdlabel = cdlabel.group(0).split("=")[1]
                cmdline.close()
                for device in client.query_by_subsystem("block"):
                    if device.has_property("ID_CDROM"):
                        dev = device.get_property("DEVNAME")
                        if system("blkid '%s'|grep -q '%s'" % (dev, cdlabel)):
                            live_dev = dev
            except:
                pass
            if not live_dev:
                # usb devices with LIVE label
                live_dev = findfs("LIVE")
    else:
        live_dev="/dev/live"

    system_closefds("mkdir -p /live")
    if not system_closefds("mount -r " + live_dev + " /live &>/dev/null"):
        # check if live device was setup under alternate locations
        if os.path.ismount("/dev/.initramfs/live"):
            system_closefds("mount -o bind /dev/.initramfs/live /live")
        elif os.path.ismount("/run/initramfs/live"):
            system_closefds("mount -o bind /run/initramfs/live /live")

    if os.path.ismount("/live"):
        return True
    else:
        return False

# mount root partition
# boot loader + kernel + initrd + LiveOS
def mount_liveos():
    if os.path.ismount("/liveos"):
        return True
    else:
        system_closefds("mkdir -p /liveos")
        if not system("mount LABEL=Root /liveos"):
            # just in case /dev/disk/by-label is not using devmapper and fails
            for dev in os.listdir("/dev/mapper"):
                if system("e2label \"/dev/mapper/" + dev + "\" 2>/dev/null|grep Root|grep -v Backup"):
                    system("rm -rf /dev/disk/by-label/Root")
                    system("ln -s \"/dev/mapper/" + dev + "\" /dev/disk/by-label/Root")
                    if system("mount LABEL=Root /liveos"):
                        return True
        else:
            return True

def mount_efi():
    efi_part = findfs("Root")
    efi_part = efi_part[:-1] + "1"
    if system_closefds("mount -t vfat " + efi_part + " /liveos/efi"):
        return True
    else:
        logger.error("Unable to mount EFI partition")
        return False

# mount config partition
# /config for persistance
def mount_config():
    # Only try to mount /config if the persistent storage exists
    if os.path.exists("/dev/HostVG/Config"):
        system_closefds("mkdir -p /config")
        if not os.path.ismount("/config"):
            ret = system_closefds("mount /dev/HostVG/Config /config")
            if ret > 0:
                return False

        # optional config embedded in the livecd image
        if os.path.exists("/live/config"):
            system_closefds("cp -rv --update /live/config/* /config")

        # bind mount all persisted configs to rootfs
        filelist_cmd = "find /config -type f"
        filelist = subprocess_closefds(filelist_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
        filelist = filelist.stdout.read()
        for f in filelist.split():
            logger.debug("Bind Mounting: " + f)
            if os.path.isfile(f) and f != "/config/files":
                target = string.replace(f, "/config", "")
                mounted_cmd = "grep -q " + target + " /proc/mounts"
                mounted = system_closefds(mounted_cmd)
                if mounted == 0:
                    # skip if already bind-mounted
                    pass
                else:
                    dirname = os.path.dirname(target)
                    system_closefds("mkdir -p '%s'" % dirname)
                    system_closefds("touch '%s'" % target)
                    system_closefds("mount -n --bind '%s' '%s'" % (f,target))
        return True
    else:
        # /config is not available
        return False

def mount_boot(self):
    if os.path.ismount("/boot"):
        return
    else:
        system_closefds("mkdir -p /boot")
        system_closefds("mount LABEL=Boot /boot")

# stop any service which keeps /var/log busy
# keep the list of services
def unmount_logging_services():
    # mapping command->service is lame, but works for most initscripts
    logging_services= []
    prgs_cmd = "cd /etc/init.d|lsof -Fc +D /var/log|grep ^c|sort -u"
    prgs = subprocess_closefds(prgs_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    prgs_output = prgs.stdout.read()
    for prg in prgs_output.split():
        svc = prg = prg[1:]
        ret = system_closefds("service " + svc +" stop &>/dev/null")
        if ret != 0:
            # make sure we don't kill ourselves.
            pids_cmd = "pidof " + svc
            pids = subprocess_closefds(pids_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
            pids_output = pids.stdout.read()
            for pid in pids_output.split():
                if int(pid) != os.getpid():
                    system_closefds("kill -9 "+ pid)
        logging_services.append(svc)
    return logging_services
    # debugging help
    #system_closefds("lsof +D /var/log")

# mount logging partition
# this only gets executed when disk is re-partitioned, HostVG/Logging is empty
def mount_logging():
    if os.path.ismount("/var/log"):
        logger.warning("Is mounted: /var/log, suppose it's tmpfs")
    if not os.path.exists("/dev/HostVG/Logging"):
        # /var/log is not available
        logger.error("The logging partion has not been created. Please create it at the main menu.")
        return False
    logger.info("Mounting log partition")
    # temporary mount-point
    log2 = tempfile.mkdtemp()
    system_closefds("mount /dev/HostVG/Logging %s" % log2)
    logging_services = unmount_logging_services()
    # save logs from tmpfs
    system_closefds("cp -av /var/log/* %s &>/dev/null" % log2)
    # save temporary log
    if os.path.exists("/tmp/ovirt.log"):
        system_closefds("{ echo 'BEGIN of temporary log' ; cat /tmp/ovirt.log; echo 'END of temporary log' ; } &>> %s/ovirt.log" % (log2))
    system_closefds("mount --move %s /var/log &>/dev/null" % log2)
    system_closefds("restorecon -r /var/log &>/dev/null")
    for srv in logging_services:
        system_closefds("service " + srv + " start &>/dev/null")
    # make sure rsyslog restarts
    system_closefds("service rsyslog restart &>/dev/null")
    return


def unmount_logging():
    if not os.path.ismount("/var/log"):
        logger.warning("Is not mounted: /var/log, returning")
        return True
    logger.info("Unmounting log partition")
    # plymouthd keeps /var/log/boot.log
    ret = system_closefds("plymouth --ping")
    if ret == 0:
        system_closefds("plymouth --quit")
    logging_services = unmount_logging_services()

    ret = system_closefds("umount /var/log &>/dev/null")
    if ret > 0:
        return ret
    for srv in logging_services:
        system_closefds("service " + srv + " start &> /dev/null")
    return

# mount data partition
def mount_data():
    if os.path.ismount("/data"):
        return

    if os.path.exists("/dev/HostVG/Data"):
        system_closefds("mkdir -p /data")
        system_closefds("mount /data")
        system_closefds("mkdir -p /data/images")
        system_closefds("mkdir -p /data/images/rhev")
        system_closefds("chown 36:36 /data/images/rhev")
        system_closefds("mkdir -p /var/lib/libvirt/images")
        system_closefds("mount /var/lib/libvirt/images")
        system_closefds("restorecon -rv /var/lib/libvirt/images &>/dev/null")
        system_closefds("mkdir -p /data/core")
        system_closefds("mkdir -p /var/log/core")
        system_closefds("mount /var/log/core")
        system_closefds("restorecon -rv /var/log/core &>/dev/null")
        return
    else:
        # /data is not available
        logger.error("The data partion has not been created. Please create it at the main menu.")
        return False

def mount_data2():
    if os.path.ismount("/data2"):
        return True

    if os.path.exists("/dev/AppVG/Data2"):
        system("mkdir -p /data2")
        system("mount /data2")

    if os.path.ismount("/data2"):
        return True
    else:
        # /data2 is not available
        logger.error("The data2 volume can not be mounted")
        return False

def md5sum(filename):
    m = hashlib.md5()
    with open(filename) as f:
        data = f.read(4096)
        while data:
            m.update(data)
            data = f.read(4096)
        return m.hexdigest()


STRING_TYPE=(str,unicode)
# persist configuration to /config
#   ovirt_store_config /etc/config /etc/config2 ...
#   copy to /config and bind-mount back

def ovirt_store_config(files):
    if is_stateless():
        return True
    if not os.path.ismount("/config"):
        logger.error("/config is not mounted")
        return False
    if isinstance(files,STRING_TYPE):
        files_list = []
        files_list.append(files)
    else:
        files_list=files
    for f in files_list:
        filename = os.path.abspath(f)
        persist_it=True
    # ensure that, if this is a directory
    # that it's not already persisted
    if os.path.isdir(filename):
        if os.path.isdir("/config/" + filename):
            logger.warn("Directory already persisted: " + filename)
            logger.warn("You need to unpersist its child directories and/or files and try again.")
            persist_it=False
            rc = 0

    # if it's a file then make sure it's not already persisted
    if os.path.isfile(filename):
        if os.path.isfile("/config/" + filename):
            md5root=md5sum(filename)
            md5stored=md5sum("/config" + filename)
            if md5root == md5stored:
                logger.warn("File already persisted: " + filename)
                persist_it=False
                rc = 0
            else:
                # persistent copy needs refresh
                if system("umount -n " + filename + " 2> /dev/null"):
                    system("rm -f /config"+ filename)
    if persist_it:
        # skip if file does not exist
        if not os.path.exists(filename):
            logger.warn("Skipping, file: " + filename + " does not exist")
        # skip if already bind-mounted
        if not check_bind_mount(filename):
            dirname = os.path.dirname(filename)
            system("mkdir -p /config/" + dirname)
            if system("cp -a " + filename + " /config"+filename):
                if not system("mount -n --bind /config"+filename+ " "+filename):
                    logger.error("Failed to persist: " + filename)
                    rc = 1
                else:
                    logger.info("File: " + filename + " persisted")
                    rc = True
        # register in /config/files used by rc.sysinit
        ret = system_closefds("grep -q \"^$" + filename +"$\" /config/files 2> /dev/null")
        if ret > 0:
            system_closefds("echo "+filename+" >> /config/files")
            logger.info("Successfully persisted: " + filename)
            rc = 0
    else:
        logger.warn(filename + " Already persisted")
        rc = 0
    if rc == 0:
        return True

def is_persisted(filename):
    abspath = os.path.abspath(filename)
    if os.path.exists("/config" + abspath):
        return True
    else:
        return False

# unmount bindmounted config files
#       unmount_config /etc/config /etc/config2 ...
#
# Use before running commands which fail on bindmounted files.
# After the file is replaced, call ovirt_store_config /etc/config ...
# to bindmount the config file again.
#

def check_bind_mount(config_file):
    bind_mount_cmd = 'grep -q "%s ext4" /proc/mounts' % config_file
    if system_closefds(bind_mount_cmd) == 0:
        return True
    else:
        return False

def unmount_config(files):
    if os.path.ismount("/config"):
        if isinstance(files,STRING_TYPE):
            files_list = []
            files_list.append(files)
        else:
            files_list=files
        for f in files_list:
            filename = os.path.abspath(f)
            if check_bind_mount(filename):
                ret = system_closefds('umount -n "%s" &>/dev/null' % filename)
                if ret == 0:
                    if os.path.exists('/config%s' % filename):
                        # refresh the file in rootfs if it was mounted over
                        if system_closefds('cp -a /config"%s" "%s" &> /dev/null' % (filename,filename)):
                            return True

# remove persistent config files
#       remove_config /etc/config /etc/config2 ...
#
def remove_config(files):
    if is_stateless():
        return True
    # if there are no persisted files then just exit
    if os.path.exists("/config/files"):
        if os.path.getsize('/config/files') == 0:
            print "There are currently no persisted files."
            return True
    if os.path.ismount("/config"):
        if isinstance(files,STRING_TYPE):
            files_list = []
            files_list.append(files)
        else:
            files_list=files
        for f in files_list:
            filename = os.path.abspath(f)
            ret = system_closefds('grep "^%s$" /config/files > /dev/null 2>&1' % filename)
            if ret == 0:
                if check_bind_mount(filename):
                    ret = system_closefds('umount -n "%s" &>/dev/null' % filename)
                    if ret == 0:
                        if os.path.isdir(filename):
                            ret = system_closefds('cp -ar /config/"%s"/* "%s"' % (filename,filename))
                            if ret > 0:
                                logger.error(" Failed to unpersist %s" % filename)
                                return False
                            else:
                                logger.info("%s successully unpersisted" % filename)
                                return True
                        else:
                            if os.path.isfile(filename):
                                # refresh the file in rootfs if it was mounted over
                                ret = system_closefds('cp -a /config"%s" "%s"' % (filename,filename))
                                if ret > 0:
                                    logger.error("Failed to unpersist %s" % filename)
                                    return False
                                else:
                                    logger.info("%s successully unpersisted" % filename)
                    # clean up the persistent store
                    system('rm -Rf /config"%s"' % filename)
                    # unregister in /config/files used by rc.sysinit
                    system('sed --copy -i "\|^%s$|d" /config/files' % filename)
                else:
                    logger.warn("%s is not a persisted file." % filename)
            else:
                logger.warn("File not explicitly persisted: %s" % filename)

# ovirt_safe_delete_config
#       ovirt_safe_delete_config /etc/config /etc/config2 ...
#
# Use to *permanently* remove persisted configuration file.
# WARNING: file is shredded and removed
#
def ovirt_safe_delete_config(files):
    if isinstance(files,STRING_TYPE):
        files_list = []
        files_list.append(files)
    else:
        files_list=files
    for f in files_list:
        filename = os.path.abspath(f)
        if check_bind_mount(filename):
            system_closefds('umount -n "%s" &>/dev/null' % filename)

        system('sed --copy -i "\|%s$|d" /config/files' % filename)

        if os.path.isdir(filename):
            for child in subprocess_closefds("ls -d '%s'" % filename, shell=True, stdout=PIPE, stderr=STDOUT).stdout.read():
                ovirt_safe_delete_config(child)
            system("rm -rf /config'%s'" % filename)
            system("rm -rf '%s'" % filename)
        else:
            system("shred -u /config'%s'" % filename)
            system("shred -u '%s'" % filename)


# compat function to handle different udev versions
def udev_info(name, query):
    # old udev command with shortopts
    udev_cmd = "udevadm info -n %s -q %s" % (name, query)
    udev = subprocess_closefds(udev_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    udev_output = udev.stdout.read()
    udev.poll()
    udev_rc = udev.returncode
    if udev_rc > 0:
        udev_cmd = "udevadm info --name=%s --query=%s" % (name, query)
        udev = subprocess_closefds(udev_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
        udev_output = udev.stdout.read()
        udev.poll()
        udev_rc = udev.returncode
    return udev_output

def get_live_disk():
    live_disk=""
    if os.path.exists("/dev/live"):
        live_disk = os.path.dirname(udev_info("/dev/live","path"))
        if "block" in live_disk:
            live_disk = os.path.basename(udev_info("/dev/live","path")).strip()
            # if dm-XX, not enough detail to map correctly
            if "dm-" in live_disk:
                live_disk = findfs("LIVE")[:-2]
    # fallback in case LIVE label point elsewhere
    elif os.path.exists("/dev/disk/by-label/LIVE"):
        live_disk = os.path.dirname(udev_info("/dev/disk/by-label/LIVE","path"))
        if "block" in live_disk:
            live_disk = os.path.basename(udev_info("/dev/disk/by-label/LIVE","path")).strip()
            # if dm-XX, not enough detail to map correctly
            if "dm-" in live_disk:
                live_disk = findfs("LIVE")[:-2]
    else:
        ret = system_closefds("losetup /dev/loop0|grep -q '\.iso'")
        if ret != 0:
            client = gudev.Client(['block'])
            version = open("/etc/default/version")
            for line in version.readlines():
                if "PACKAGE" in line:
                    pkg, pkg_name = line.split("=")
            for device in client.query_by_subsystem("block"):
                if device.has_property("ID_CDROM"):
                    dev = device.get_property("DEVNAME")
                    blkid_cmd = "blkid '%s'|grep -q '%s' " % (dev, pkg_name)
                    ret = system_closefds(blkid_cmd)
                    if ret == 0:
                        live_disk = os.path.basename(dev)
    return live_disk

# reboot wrapper
#   cleanup before reboot

def finish_install():
    logger.info("Completing Installation")
    if not OVIRT_VARS.has_key("OVIRT_ISCSI_ENABLED"):
        # setup new Root if update is prepared
        root_update_dev = findfs("RootUpdate")
        root_dev = findfs("Root")
        e2label_rootbackup_cmd = "e2label '%s' RootBackup" % root_dev
        e2label_root_cmd = "e2label '%s' Root" % root_update_dev
        logger.debug(e2label_rootbackup_cmd)
        logger.debug(e2label_root_cmd)
        subprocess_closefds(e2label_rootbackup_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
        subprocess_closefds(e2label_root_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    # run post-install hooks
    # e.g. to avoid reboot loops using Cobbler PXE only once
    # Cobbler XMLRPC post-install trigger (XXX is there cobbler SRV record?):
    # wget "http://192.168.50.2/cblr/svc/op/trig/mode/post/system/$(hostname)"
    #   -O /dev/null
    hookdir="/etc/ovirt-config-boot.d"
    for hook in os.listdir(hookdir):
        if not is_auto_install():
            system_closefds(os.path.join(hookdir,hook))
    for f in ["/etc/ssh/ssh_host%s_key" % t for t in ["", "_dsa", "_rsa"]]:
        ovirt_store_config(f)
        ovirt_store_config("%s.pub" % f)
    # store keyboard config
    ovirt_store_config("/etc/sysconfig/keyboard")
    ovirt_store_config("/etc/vconsole.conf")
    ovirt_store_config("/var/lib/random-seed")
    return True

def is_valid_ipv4(ip_address):
    try:
        socket.inet_pton(socket.AF_INET, ip_address)
        return True
    except socket.error:
        return False

def is_valid_ipv6(ip_address):
    try:
        socket.inet_pton(socket.AF_INET6, ip_address)
        return True
    except socket.error:
        return False

def is_valid_hostname(hostname):
    regex_1 = "([a-zA-Z]|[0-9])(([a-zA-Z]|[0-9]|-)*([a-zA-Z]|[0-9]))?$"
    regex_2 = "^(([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9])\.)*([A-Za-z]|[A-Za-z][A-Za-z0-9\-]*[A-Za-z0-9])$"
    if re.match(regex_1, hostname):
        return True
    else:
        if re.match(regex_2, hostname):
            return True
        else:
            return False

def is_valid_nfs(nfs_entry):
    regex = "^([a-zA-Z0-9_\-]+)([\.][a-zA-Z0-9_\-]+)+([:][/][a-zA-Z0-9\~\(\)_\-]*)+([\.][a-zA-Z0-9\(\)_\-]+)*"
    if re.match(regex, nfs_entry):
        ip = re.findall(r'[0-9]+(?:\.[0-9]+){3}', nfs_entry)
        try:
            if ip[0]:
                if is_valid_ipv4(ip[0]):
                    return True
                else:
                    return False
        except:
            # hostname will fail on purpose
            return True
    else:
        return False

def is_valid_host_port(host):
    regex = "^([a-zA-Z0-9_\-]+)([\.][a-zA-Z0-9_\-]+)+([:][0-9\~\(\)_\-]*)+([\.][0-9]+)*$"
    if re.match(regex, host):
        return True
    else:
        return False

def is_valid_url(host):
    regex = "(((http|https)://)|(www\.))+(([a-zA-Z0-9\._-]+\.[a-zA-Z]{2,6})|([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}))(/[a-zA-Z0-9\&amp;%_\./-~-]*)?"
    if re.match(regex, host):
        return True
    else:
        return False

def is_valid_host_or_ip(host_or_ip):
    if host_or_ip != "":
        if is_valid_ipv4(host_or_ip):
            return True
        if is_valid_ipv6(host_or_ip):
            return True
        if is_valid_hostname(host_or_ip):
            return True
        else:
            return False
    else:
        return True

def is_valid_user_host(user):
    regex = "^[\w-]+(\.[\w-]+)*@([a-z0-9-]+(\.[a-z0-9-]+)*?\.[a-z]{2,6}|(\d{1,3}\.){3}\d{1,3})(:\d{4})?$"
    if re.match(regex, user):
        return True
    else:
        return False

def is_valid_iqn(iqn):
    regex="^iqn\.(\d{4}-\d{2})\.([^:]+):"
    if re.match(regex, iqn):
        return True
    else:
        return False

# Check if networking is already up
def network_up():
    ret = system_closefds("ip addr show | grep -q 'inet.*scope global'")
    if ret == 0:
        return True
    return False

def get_ip_address(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ip = socket.inet_ntoa(fcntl.ioctl(
            s.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack('256s', ifname[:15])
        )[20:24])
    except:
        ip = ""
    return ip

def get_netmask(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        netmask = fcntl.ioctl(s, 0x891b, struct.pack('256s', ifname))[20:24]
        netmask = socket.inet_ntoa(netmask)
    except:
        netmask = ""
    return netmask

def get_gateway(ifname):
    cmd = "ip route list dev "+ ifname + " | awk ' /^default/ {print $3}'"
    result = subprocess_closefds(cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    result = result.stdout.read().strip()
    return result

def get_ipv6_address(interface):
    inet6_lookup_cmd = "ip addr show dev %s | awk '$1==\"inet6\" && $4==\"global\" { print $2 }'" % interface
    inet6_lookup = subprocess_closefds(inet6_lookup_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    ipv6_addr = inet6_lookup.stdout.read().strip()
    try:
        ip, netmask = ipv6_addr.split("/")
        return (ip,netmask)
    except:
        logger.debug("unable to determine ip/netmask from: " + ipv6_addr)
    return False

def get_ipv6_gateway(ifname):
    cmd = "ip route list dev "+ ifname + " | awk ' /^default/ {print $3}'"
    result = subprocess_closefds(cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    result = result.stdout.read().strip()
    return result

def has_ip_address(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(),
            0x8915, struct.pack('256s', ifname[:15]))[20:24])
        return True
    except IOError:
        return False

def is_valid_port(port_number):
    regex = "^(6553[0-5]|655[0-2]\d|65[0-4]\d\d|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3}|0)$"
    if re.match(regex, port_number):
        return True
    else:
        return False

# Cleans partition tables
def wipe_partitions(drive):
    logger.info("Removing HostVG")
    if os.path.exists("/dev/mapper/HostVG-Swap"):
        system_closefds("swapoff -a")
    # remove remaining HostVG entries from dmtable
    for lv in os.listdir("/dev/mapper/"):
        if "HostVG" in lv:
            system_closefds("dmsetup remove " +lv + " &>>" + OVIRT_TMP_LOGFILE)
    logger.info("Wiping old boot sector")
    system_closefds("dd if=/dev/zero of=\""+ drive +"\" bs=1024K count=1 &>>" + OVIRT_TMP_LOGFILE)
    # zero out the GPT secondary header
    logger.info("Wiping secondary gpt header")
    disk_kb = subprocess_closefds("sfdisk -s \""+ drive +"\" 2>/dev/null", shell=True, stdout=PIPE, stderr=STDOUT)
    disk_kb_count = disk_kb.stdout.read()
    system_closefds("dd if=/dev/zero of=\"" +drive +"\" bs=1024 seek=$(("+ disk_kb_count+" - 1)) count=1 &>>" + OVIRT_TMP_LOGFILE)
    system_closefds("sync")

def test_ntp_configuration(self):
    # stop ntpd service for testing
    system_closefds("service ntpd stop > /dev/null 2>&1")
    for server in OVIRT_VARS["NTP"].split():
        ret = system_closefds("ntpdate %s > /dev/null 2>&1" % server)
        if ret > 0:
            logger.error("Unable to verify NTP server: %s" % server)
        else:
            logger.info("Verified NTP server: %s" % server)
    system_closefds("service ntpd start")

def get_dm_device(device):
    dev_major_cmd="stat -c '%t' " + "\"/dev/" + device + "\""
    dev_minor_cmd="stat -c '%T' " + "\"/dev/" + device + "\""
    major_lookup = subprocess_closefds(dev_major_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    minor_lookup = subprocess_closefds(dev_minor_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    major_lookup = major_lookup.stdout.read().strip()
    minor_lookup = minor_lookup.stdout.read().strip()
    dm_cmd = "ls /dev/mapper"
    dm_cmd = subprocess_closefds(dm_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    devices = dm_cmd.stdout.read().strip()
    for dm in devices.split("\n"):
        dm_major_cmd="stat -c '%t' " + "\"/dev/mapper/" + dm + "\""
        dm_minor_cmd="stat -c '%T' " + "\"/dev/mapper/" + dm + "\""
        dm_major_lookup = subprocess_closefds(dm_major_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
        dm_minor_lookup = subprocess_closefds(dm_minor_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
        dm_major_lookup = dm_major_lookup.stdout.read().strip()
        dm_minor_lookup = dm_minor_lookup.stdout.read().strip()
        if dm_major_lookup == major_lookup and minor_lookup == dm_minor_lookup:
            dm = "/dev/mapper/" + dm
            return dm

def check_existing_hostvg(install_dev):
    if install_dev is "":
        devices_cmd = "pvs --separator=\":\" -o pv_name,vg_name --noheadings 2>/dev/null| grep HostVG |awk -F \":\" {'print $1'}"
    else:
        devices_cmd="pvs --separator=: -o pv_name,vg_name --noheadings 2>/dev/null| grep -v '%s' | grep HostVG | awk -F: {'print $1'}" % install_dev
    devices_cmd = subprocess_closefds(devices_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    devices = devices_cmd.stdout.read().strip()
    if len(devices) > 0:
        logger.error("There appears to already be an installation on another device:")
        for device in devices.split(":"):
            logger.error(device)
        logger.error("The installation cannot proceed until the device is removed")
        logger.error("from the system of the HostVG volume group is removed")
        return devices
    else:
        return False

def translate_multipath_device(dev):
    #trim so that only sdX is stored, but support passing /dev/sdX
    logger.debug("Translating: %s" % dev)
    if dev is None:
        return False
    if "/dev/mapper" in dev:
        return dev
    if "/dev/cciss" in dev:
        cciss_dev_cmd = "cciss_id " + dev
        cciss_dev = subprocess_closefds(cciss_dev_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
        dev = "/dev/mapper/" + cciss_dev.stdout.read().strip()
    dm_dev_cmd = "multipath -ll '%s' | egrep dm-[0-9]+" % dev
    dm_dev = subprocess_closefds(dm_dev_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    (dm_dev_output, dummy) = dm_dev.communicate()
    if dm_dev.returncode > 0:
        return dev
    else:
        logger.debug("Translated to: /dev/mapper/" + dm_dev_output.split()[0])
        return "/dev/mapper/"+dm_dev_output.split()[0]

def pwd_lock_check(user):
    passwd_cmd = "passwd -S %s" % user
    passwd = subprocess_closefds(passwd_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    passwd, err = passwd.communicate()
    if "locked" in passwd:
        return True
    else:
        return False

def pwd_set_check(user):
    passwd_cmd = "passwd -S %s" % user
    passwd = subprocess_closefds(passwd_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    passwd, err = passwd.communicate()
    if "set" in passwd:
        return True
    else:
        return False

def get_installed_version_number():
    if mount_liveos():
        existing_version = open("/liveos/version")
        existing_install = {}
        for line in existing_version.readlines():
            try:
                key, value = line.strip().split("=")
                value = value.replace("'", "")
                existing_install[key] = value
            except:
                pass
        if existing_install.has_key("VERSION") and existing_install.has_key("RELEASE"):
            return [existing_install["VERSION"],existing_install["RELEASE"]]
        else:
            return False

def get_media_version_number():
    new_install = {}
    if mount_live():
        try:
            upgrade_version = open("/live/isolinux/version")
        except:
            upgrade_version = open("/live/syslinux/version")
        for line in upgrade_version.readlines():
            try:
                key, value = line.strip().split("=")
                value = value.replace("'", "")
                new_install[key] = value
            except:
                pass
    else:
        logger.error("Failed to mount_live()")
        return False
    if new_install.has_key("VERSION") and new_install.has_key("RELEASE"):
        return [new_install["VERSION"],new_install["RELEASE"]]
    return False

def findfs(label):
    system("partprobe /dev/mapper/*")
    system("udevadm settle")
    blkid_cmd = "/sbin/blkid -c /dev/null -l -o device -t LABEL=\"" + label + "\""
    blkid = subprocess_closefds(blkid_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    blkid_output = blkid.stdout.read().strip()
    return blkid_output

def system(command):
    system_cmd = subprocess_closefds(command, shell=True, stdout=PIPE, stderr=PIPE)
    output, err = system_cmd.communicate()
    logger.propagate = False
    logger.debug(command)
    logger.debug(output)
    if system_cmd.returncode == 0:
        return True
    else:
        return False

def password_check(password_1, password_2, min_length=1):
    '''
    Do some password checks

    >>> r, msg = password_check("", "")
    >>> (r,  len(msg))
    (1, 5)
    >>> r, msg = password_check("foo", "bar")
    >>> (r,  "Not Match" in msg)
    (1, True)
    '''
    num_o_lines_to_expand = 6
    accepted = False
    message = ""

    if is_capslock_on():
        message = "Hint: Caps lock is on.\n"

    if len(password_1) is 0 and min_length is not 0:
        message += ""  # Intentional dummy
    elif len(password_1) < min_length:
        message += "Password must be at least %d characters" % min_length
    elif password_1 != "" and password_2 == "":
        message += "Please Confirm Password"
    elif password_1 != password_2:
        message += "Passwords Do Not Match"
    else:
        try:
            cracklib.FascistCheck(password_1)
            accepted = True
        except ValueError, e:
            message += "You have provided a weak password!\n"
            message += "Strong passwords contain a mix of uppercase,\n"
            message += "lowercase, numeric and punctuation characters.\n"
            message += "They are six or more characters long and\n"
            message += "do not contain dictionary words"
            accepted = True

    num_lines = message.count("\n") + 1

    # Modify message to span num_o_lines_to_expand lines
    message += (num_o_lines_to_expand - num_lines) * "\n"
    accepted = 0 if accepted else 1
    return (accepted, message)

def get_logrotate_size():
    size = augtool_get("/files/etc/logrotate.d/ovirt-node/rule/size")
    if "m" in size.lower():
        multiplier = 1024
    else:
        multiplier = 1
    size = size.lower().rstrip("kmb")
    size = int(size) * multiplier
    return str(size)

def get_cpu_flags():
    cpuflags_cmd = "cat /proc/cpuinfo |grep ^flags|tail -n 1"
    cpuflags_lookup = subprocess_closefds(cpuflags_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    return cpuflags_lookup.stdout.read().strip()

def kvm_enabled():
    try:
        conn = libvirt.openReadOnly(None)
        libvirt_capabilities = conn.getCapabilities()
    except:
        return 0
    if "kvm" in libvirt_capabilities:
        return 1
    else:
        return 2

def virt_cpu_flags_enabled():
    cpuflags = get_cpu_flags()
    if "vmx" in cpuflags or "svm" in cpuflags:
        return True
    else:
        return False

def get_virt_hw_status():
    hwvirt_msg = ""
    kvm_status = kvm_enabled()
    if kvm_status == 0:
        return "(Failed to Establish Libvirt Connection)"
    elif kvm_status == 1:
        logger.info("Hardware virtualization detected")
    elif kvm_status == 2:
        hwvirt_msg = "Virtualization hardware is unavailable."
        if virt_cpu_flags_enabled():
            hwvirt_msg = "(Virtualization hardware detected but disabled)"
        else:
            hwvirt_msg = "(Virtualization hardware was not detected)"
    return hwvirt_msg


def cpu_details():
    status_msg = ""
    cpu_info_cmd = "cat /proc/cpuinfo"
    cpu_info = subprocess.Popen(cpu_info_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    cpu_info = cpu_info.stdout.read().strip()
    cpu_dict = {}
    for line in cpu_info.splitlines():
        try:
            key, value = line.split(":")
            cpu_dict[key.replace("\t","")] = value
        except:
            pass
    # get capabilities from libvirt
    try:
        conn = libvirt.openReadOnly(None)
        libvirt_capabilities = conn.getCapabilities()
    except:
        return "(Failed to Establish Libvirt Connection)"
    dom = parseString(libvirt_capabilities)
    vendorTag = dom.getElementsByTagName('vendor')[0].toxml()
    modelTag = dom.getElementsByTagName('model')[0].toxml()
    topologyTag = dom.getElementsByTagName('topology')[0].toxml()
    cpu_model = modelTag.replace('<model>','').replace('</model>','')
    cpu_vendor = vendorTag.replace('<vendor>','').replace('</vendor>','')
    cpu_topology = topologyTag.replace('<topology>','').replace('</topology>','').split()
    status_msg += "CPU Name: %s\n" % cpu_dict["model name"].replace("  "," ")
    status_msg += "CPU Type: %s %s\n" % (cpu_vendor, cpu_model)
    if kvm_enabled() and virt_cpu_flags_enabled():
        status_msg += "Virtualization Extensions Enabled: Yes\n"
    else:
        status_msg += "Virtualization Extensions Enabled: \n%s\n" \
            % get_virt_hw_status()
    if cpu_vendor == "Intel":
        if "nx" in cpu_dict["flags"]:
            status_msg += "NX Flag: Yes\n"
        else:
            status_msg += "NX Flag: No\n"
    if cpu_vendor == "AMD":
        if "evp" in cpu_dict["flags"]:
            status_msg += "EVP Flag: Yes\n"
        else:
            status_msg += "EVP Flag: No\n"
    cpu_sockets=cpu_topology[2].split("=")[1].replace('"',"")
    status_msg += "CPU Sockets: %s\n" % cpu_sockets
    cpu_cores=cpu_topology[1].split("=")[1].replace('"',"")
    status_msg += "CPU Cores: %s\n" % cpu_cores
    return status_msg

def get_ssh_hostkey(variant="rsa"):
    fn_hostkey = "/etc/ssh/ssh_host_%s_key.pub" % variant
    hostkey = open(fn_hostkey).read()
    hostkey_fp_cmd = "ssh-keygen -l -f '%s'" % fn_hostkey
    hostkey_fp_lookup = subprocess_closefds(hostkey_fp_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    fingerprint = hostkey_fp_lookup.stdout.read().strip().split(" ")[1]
    return (fingerprint, hostkey)

def get_mac_address(dev):
    nic_addr_file = open("/sys/class/net/" + dev + "/address")
    dev_address = nic_addr_file.read().strip()
    return dev_address

def logical_to_physical_networks():
    networks = {}
    client = gudev.Client(['net'])
    for device in client.query_by_subsystem("net"):
        try:
            dev_interface = device.get_property("INTERFACE")
            dev_address = get_mac_address(dev_interface)
            bridge_cmd = "/files/etc/sysconfig/network-scripts/ifcfg-%s/BRIDGE" % str(dev_interface)
            dev_bridge = augtool_get(bridge_cmd)
        except:
            pass
        if not dev_bridge is None:
            networks[dev_bridge] = (dev_interface,dev_address)
    return networks

def has_fakeraid(device):
    fakeraid_cmd = "dmraid -r $(readlink -f \"" + device + "\")"
    fakeraid = subprocess_closefds(fakeraid_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    fakeraid.communicate()
    fakeraid.poll()
    if fakeraid.returncode == 0:
        return True
    else:
        return False

def is_wipe_fakeraid():
    OVIRT_VARS = parse_defaults()
    # check if theres a key first
    if "OVIRT_WIPE_FAKERAID" in OVIRT_VARS:
        if OVIRT_VARS["OVIRT_WIPE_FAKERAID"] == "1":
            return True
        elif OVIRT_VARS["OVIRT_WIPE_FAKERAID"] == "0":
            return False
    return False

def set_wipe_fakeraid(value):
    augtool("set","/files/etc/default/ovirt/OVIRT_WIPE_FAKERAID",str(value))
    OVIRT_VARS = parse_defaults()


def pad_or_trim(length, string):
    to_rem = len(string) - length
    # if negative pad name space
    if to_rem < 0:
        while abs(to_rem) != 0:
            string = string + " "
            to_rem = to_rem + 1
    else:
        string = string.rstrip(string[-to_rem:])
    return string

def is_efi_boot():
    if os.path.exists("/sys/firmware/efi"):
        return True
    else:
        return False

def manage_firewall_port(port, action="open", proto="tcp"):
    if action == "open":
        opt = "-A"
        logger.info("Opening port " + port)
    elif action == "close":
        opt = "-D"
        logger.info("Closing port " + port)
    system_closefds("iptables %s INPUT -p %s --dport %s -j ACCEPT" % (opt, proto, port))
    # service iptables save can not be used, bc of mv on bind mounted file
    system_closefds("iptables-save -c > /etc/sysconfig/iptables")
    ovirt_store_config("/etc/sysconfig/iptables")

def is_iscsi_install():
    if OVIRT_VARS.has_key("OVIRT_ISCSI_INSTALL") and OVIRT_VARS["OVIRT_ISCSI_INSTALL"].upper() == "Y":
        return True
    else:
        return False

def load_keyboard_config():
    import system_config_keyboard.keyboard as keyboard
    kbd = keyboard.Keyboard()
    kbd.read()
    kbd.activate()

def is_engine_configured():
    '''
    Checks if the rhevm bridge is there, an indicator if we are managed
    by engine.
    A simple doctest:

    >>> bridge_file = os.path.exists("/etc/sysconfig/network-scripts/" + \
                                     "ifcfg-rhevm")
    >>> bridge_test = is_engine_configured()
    >>> bridge_file is bridge_test
    True
    '''
    if system_closefds("brctl show | egrep -iq 'ovirtmgmt|rhevm'") is 0:
        return True
    else:
        return False

def create_minimal_etc_hosts_file():
    filename = "/etc/hosts"
    if open(filename, "r").read().strip() == "":
        logger.info("Won't update %s, it's not empty." % filename)
        return
    if not is_persisted(filename):
        logger.warning("Want but can't update %s, it's not persisted." % filename)
        return
    with open(filename, "w") as f:
        f.write("""
# Created by create_minimal_etc_hosts_file
127.0.0.1		localhost.localdomain localhost
::1		localhost6.localdomain6 localhost6
""")

def nic_link_detected(iface):
    link_status_cmd = "ip link set dev {dev} up ; ethtool {dev} |grep \"Link detected\"".format(dev=iface)
    link_status = subprocess_closefds(link_status_cmd, shell=True, stdout=PIPE, stderr=STDOUT)
    link_status = link_status.stdout.read()
    return ("yes" in link_status)

def is_capslock_on():
    """Returns True if Caps Lock is on.
    """
    tty =  get_ttyname()
    if "S" in tty:
        # It is assumed to be a serial console, we can't get the state of
        # CapsLock, so return nothing
        return None
    cmd = "LC_ALL=C setleds < /dev/%s | awk '/Current flags:/{print $6;}'" % tty
    return "on" == subprocess_closefds(cmd, shell=True, stdout=PIPE, \
                                       stderr=STDOUT).stdout.read().strip()
def rng_status():
    bit_value = 0
    disable_aes_ni = 0
    try:
        with open("/etc/profile") as f:
            for line in f:
                try:
                    if "SSH_USE_STRONG_RNG" in line:
                        export , kv = line.split()
                        key, bit_value = kv.split("=")
                    elif "OPENSSL_DISABLE_AES_NI=" in line:
                        disable_aes_ni = 1
                except:
                    pass
    except:
        pass
    return (bit_value, disable_aes_ni)

class PluginBase(object):
    """Base class for pluggable Hypervisor configuration options.

    Configuration plugins are modules in ovirt_config_setup package.
    They provide implementation of this base class, adding specific
    form elements and processing.
    """

    def __init__(self, name, screen):
        """Initialize a PluginBase instance

        name -- configuration option label
        screen -- parent NodeConfigScreen
        """
        self.name = name
        """A name of the configuration option."""
        self.ncs = screen
        """A NodeConfigScreen instance."""

    def label(self):
        """Returns label for the configuration option."""
        return self.name

    def form(self):
        """Returns form elements for the configuration option.
        Must be implemented by the child class.
        """
        pass

    def action(self):
        """Form processing action for the Hypervisor configuration option.
        Must be implemented by the child class.
        """
        pass

OVIRT_VARS = parse_defaults()

# setup logging facility
if is_stateless():
    log_file = OVIRT_LOGFILE
elif is_firstboot():
    log_file = OVIRT_TMP_LOGFILE
else:
    log_file = OVIRT_LOGFILE

def setup_custom_logger():
    formatter = logging.Formatter(fmt='%(asctime)s - %(levelname)s - %(module)s - %(message)s')
    handler = logging.FileHandler(log_file)
    handler.setFormatter(formatter)
    logger = logging.getLogger(PRODUCT_SHORT)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger

setup_custom_logger()
logger = logging.getLogger(PRODUCT_SHORT)