#!/usr/bin/python
#
# tui.py - Copyright (C) 2012 Red Hat, Inc.
# Written by Fabian Deutsch <fabiand@redhat.com>
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

"""
A visitor to build the urwid TUI from the abstract UI definitions.
Is based on the visitor pattern
"""

import urwid

import logging

import ovirt.node
import ovirt.node.plugins
import ovirt.node.ui
import ovirt.node.ui.widgets
import ovirt.node.ui.builder
import ovirt.node.exceptions
import ovirt.node.utils

LOGGER = logging.getLogger(__name__)


def page_from_plugin(tui, plugin):
    element = plugin.ui_content()
    widget = None

    # FIXME could also be done using dict.
    if type(element) is ovirt.node.ui.Page:
        widget = build_page(tui, plugin, element)
    else:
        raise Exception("Unknown element container: %s" % element)

    return widget


def build_page(tui, plugin, container):
    widgets = []

    # Always create the SaveButton, but only display it if requested
    #save = ovirt.node.ui.widgets.Button("Save")
    #urwid.connect_signal(save, 'click', lambda x: plugin._on_ui_save())
    save = build_button("", ovirt.node.ui.SaveButton(), tui, plugin)
    plugin._save_button = save

    for path, item in container.widgets:
        widget = widget_for_item(tui, plugin, path, item)
        widgets.append(("flow", widget))

    if container.has_save_button:
        widgets.append(urwid.Filler(save))

    widgets.append(urwid.Filler(urwid.Text("")))

    LOGGER.debug("Triggering initial sematic checks for '%s'" % plugin)
    try:
        plugin.check_semantics()
    except:
        tui.notify("error", "Initial model validation failed.")

    page = ovirt.node.ui.widgets.PageWidget(widgets)

    return page


def widget_for_item(tui, plugin, path, item):
    item_to_builder = {
        ovirt.node.ui.Label: build_label,
        ovirt.node.ui.Header: build_label,
        ovirt.node.ui.KeywordLabel: build_label,
        ovirt.node.ui.Entry: build_entry,
        ovirt.node.ui.PasswordEntry: build_entry,
        ovirt.node.ui.Button: build_button,
        ovirt.node.ui.SaveButton: build_button,
        ovirt.node.ui.Divider: build_divider,
        ovirt.node.ui.Options: build_options,
    }

    # Check if builder is available for UI Element
    assert type(item) in item_to_builder.keys(), \
           "No widget for item type"

    # Build widget from UI Element
    build_func = item_to_builder[type(item)]
    widget = build_func(path, item, tui, plugin)

    # Populate with values
    if type(item) in [ovirt.node.ui.Entry,
                      ovirt.node.ui.PasswordEntry,
                      ovirt.node.ui.KeywordLabel,
                      ovirt.node.ui.Options]:
        widget.set_text(plugin.model()[path])

    return widget


def build_entry(path, item, tui, plugin):
    widget = None
    if type(item) is ovirt.node.ui.Entry:
        widget = ovirt.node.ui.widgets.Entry(item.label)
    else:
        widget = ovirt.node.ui.widgets.PasswordEntry(item.label)

    widget.enable(item.enabled)

    def on_item_enabled_change_cb(w, v):
        LOGGER.debug("Model changed, updating widget '%s': %s" % (w,
                                                                  v))
        if widget.selectable() != v:
            widget.enable(v)
    item.connect_signal("enabled", on_item_enabled_change_cb)

    def on_widget_value_change(widget, new_value):
        LOGGER.debug("Widget changed, updating model '%s'" % path)

        try:
            change = {path: new_value}
            plugin.validate(change)
            plugin._on_ui_change(change)
            widget.notice = ""
            widget.valid(True)
            LOGGER.debug(plugin.__dict__)
            plugin._save_button.enable(True)

        except ovirt.node.exceptions.Concern as e:
            LOGGER.error("Concern when updating: %s" % e)

        except ovirt.node.exceptions.InvalidData as e:
            LOGGER.error("Invalid data when updating: %s" % e)
            widget.notice = e.message
            widget.valid(False)
            plugin._save_button.enable(False)

        # FIXME page validation must happen within tui, not plugin
        # as UI data should be handled in tui

        tui.draw_screen()
    urwid.connect_signal(widget, 'change', on_widget_value_change)

    return widget


def build_label(path, item, tui, plugin):
    if type(item) is ovirt.node.ui.KeywordLabel:
        widget = ovirt.node.ui.widgets.KeywordLabel(item.keyword,
                                                    item.text())
    elif type(item) is ovirt.node.ui.Header:
        widget = ovirt.node.ui.widgets.Header(item.text())
    else:
        widget = ovirt.node.ui.widgets.Label(item.text())

    def on_item_text_change_cb(w, v):
        LOGGER.debug("Model changed, updating widget '%s': %s" % (w,
                                                                  v))
        widget.text(v)
        # Redraw the screen if widget text is updated "outside" of the
        # mainloop
        tui.draw_screen()
    item.connect_signal("text", on_item_text_change_cb)

    return widget


def build_button(path, item, tui, plugin):
    widget = ovirt.node.ui.widgets.Button(item.text())

    def on_widget_click_cb(widget, data=None):
        LOGGER.debug("Button click: %s" % widget)
        if type(item) is ovirt.node.ui.SaveButton:
            r = plugin._on_ui_save()
            LOGGER.debug("Got save: %s" % r)
            # FIXME hacks to display page or dialog
            if type(r) in [ovirt.node.ui.Page]:
                w = build_page(tui, plugin, r)
                tui.display(w)
            elif type(r) in [ovirt.node.ui.Dialog]:
                w = build_page(tui, plugin, r)
                tui._display_dialog(w, r.title)
        else:
#           Not propagating the signal as a signal to the plugin
#           item.emit_signal("click", widget)
            plugin._on_ui_change({path: True})
    urwid.connect_signal(widget, "click", on_widget_click_cb)

    return widget


def build_divider(path, item, tui, plugin):
    return ovirt.node.ui.widgets.Divider(item.char)


def build_options(path, item, tui, plugin):
    widget = ovirt.node.ui.widgets.Options(item.label, item.options,
                                           plugin.model()[path])

    def on_widget_change_cb(widget, data):
        LOGGER.debug(data)
        item.option(data)
        plugin._on_ui_change({path: data})
    urwid.connect_signal(widget, "change", on_widget_change_cb)

    return widget