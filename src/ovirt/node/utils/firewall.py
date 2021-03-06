#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# firewall.py - Copyright (C) 2013 Red Hat, Inc.
# Written by Joey Burns <jboggs@redhat.com>
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

import os
from ovirt.node.utils import process
from glob import glob

PLUGIN_DIR = "/etc/ovirt-plugins.d/"
PLUGIN_XML_OUT = "/etc/firewalld/services/node-plugin.xml"
plugin_files = []
fw_conf = []

FIREWALLD_PORT_XML = """<port protocol="%(proto)s" port="%(port)s"/>\n  """

FIREWALLD_XML_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<service>
  <short>firewall plugin</short>
  <description>necessary ports for ovirt-node plugin operations</description>
  %(port_section)s
</service>
"""


def is_firewalld():
    if os.path.exists("/etc/firewalld"):
        return True
    else:
        return False


def setup_iptables(port, proto):
        cmd = ["iptables", "-I", "INPUT", "1", "-p", proto,
               "--dport", port, "-j", "ACCEPT"]
        process.check_call(cmd, shell=True)


def setup_firewalld(port, proto):
    port_conf = ""
    rule_dict = {"port": port,
                 "proto": proto
                 }

    port_conf += FIREWALLD_PORT_XML % rule_dict
    port_dict = {"port_section": port_conf}
    with open(PLUGIN_XML_OUT, "w") as f:
        f.write(FIREWALLD_XML_TEMPLATE % port_dict)

    process.call(["firewall-cmd", "--reload"])
    process.call(["firewall-cmd", "--permanent", "--add-service",
                  "node-plugin"])
    process.check_call(["firewall-cmd", "--reload"])


def process_plugins():
    for plugin in glob(PLUGIN_DIR + "*.firewall"):
        plugin_files.append(plugin)

    for f in plugin_files:
        with open(f) as i:
            conf = i.readlines()
        for line in conf:
            if not line.startswith("#"):
                port, proto = line.strip().split(",")
                fw_conf.append((port, proto))

    for i in fw_conf:
        port, proto = i
        if is_firewalld():
            setup_firewalld(port, proto)
        else:
            setup_iptables(port, proto)


if __name__ == "__main__":
    process_plugins()
