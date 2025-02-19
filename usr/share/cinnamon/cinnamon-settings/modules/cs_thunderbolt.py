#!/usr/bin/python3

import os

from SettingsWidgets import SidePage
from xapp.GSettingsWidgets import *
from gi.repository import *

def build_info_row(key, value):
    row = SettingsWidget()
    labelKey = Gtk.Label.new(key)
    labelKey.get_style_context().add_class("dim-label")
    row.pack_start(labelKey, False, False, 0)
    labelValue = Gtk.Label.new(value)
    labelValue.set_selectable(True)
    labelValue.set_line_wrap(True)
    row.pack_end(labelValue, False, False, 0)
    return row

def build_listbox_row(key, value):
    content = build_info_row(key, value)
    row = Gtk.ListBoxRow(can_focus=False)
    row.add(content)
    return row

def format_generation(gen):
    if gen in (1,2,3):
        return f'Thunderbolt {gen}'
    elif gen == 4:
        return 'USB4'
    raise ValueError("undefined thunderbolt generation")

class BoltDevice:
    def __init__(self, proxy, trust_callback, forget_callback):
        self._proxy = proxy
        self._trust = trust_callback
        self._forget = forget_callback
        self.name = proxy.get_cached_property('Name').unpack()
        self.type = proxy.get_cached_property('Type').unpack()
        self.vendor = proxy.get_cached_property('Vendor').unpack()
        self.uid = proxy.get_cached_property('Uid').unpack()
        self.generation = proxy.get_cached_property('Generation').unpack()
        self.status = proxy.get_cached_property('Status').unpack()
        self.stored = proxy.get_cached_property('Stored').unpack()
        linkspeed = proxy.get_cached_property('LinkSpeed').unpack()
        speed = linkspeed['tx.speed']
        lanes = linkspeed['tx.lanes']
        self.bandwidth = f'{lanes * speed} Gb/s ({lanes} lanes @ {speed} Gb/s)'

        # Build the widgets for this bolt device
        self._init_widgets()
        
        # Use this signal to key into device status changes
        self._proxy.connect('g-properties-changed', self._on_prop_changes)

    def _init_widgets(self):
        # Build the status label
        self.status_label = Gtk.Label.new()

        # Build the Details/Authorize/Trust button box
        self._btn_auth = Gtk.Button.new_with_label(_("Authorize"))
        self._btn_auth.connect('clicked', self._on_btn_auth_click)
        self._btn_trust = Gtk.Button.new()
        self._btn_trust.connect('clicked', self._on_btn_trust_click)
        btn_details = Gtk.ToggleButton.new_with_label(_("Details"))
        btn_details.connect('toggled', self._on_btn_details_toggled)
        self.buttons = Gtk.ButtonBox.new(Gtk.Orientation.HORIZONTAL)
        self.buttons.pack_start(btn_details, True, True, 0)
        self.buttons.pack_start(self._btn_auth, True, True, 0)
        self.buttons.pack_start(self._btn_trust, True, True, 0)
        self.buttons.set_layout(Gtk.ButtonBoxStyle.EXPAND)

        # Build the details revealer
        self.revealer = Gtk.Revealer.new()
        # This initialization is taken from SettingsRevealer class in python3-xapp
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.revealer.set_transition_duration(150)
        self.revealer.set_reveal_child(False)

        # Build the details, pack into the revealer
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add(build_listbox_row('Generation', format_generation(self.generation)))
        list_box.add(build_listbox_row('Bandwidth', self.bandwidth))
        list_box.add(build_listbox_row('Type', self.type))
        list_box.add(build_listbox_row('UID', self.uid))
        self.details_box = list_box

        # Refresh the dyanmic widgets
        self._refresh()

    def _refresh(self):
        # Refresh widgets based on current state
        text = _("Disconnected")
        if self.status in ("connected", "authorizing", "authorized"):
            text = _("Connected")
        if self.status == "authorized":
            text = text + " & " + _("Authorized")

        if self.stored:
            text = text + ", " + _("Trusted")
            self._btn_trust.set_label(_("Forget"))
        else:
            self._btn_trust.set_label(_("Trust"))
        self.status_label.set_label(text)

        if self.status == 'connected':
            self._btn_auth.set_sensitive(True)
            self._btn_trust.set_sensitive(False)
        else:
            self._btn_auth.set_sensitive(False)
            self._btn_trust.set_sensitive(True)

    def _on_btn_details_toggled(self, button):
        self.revealer.set_reveal_child(button.get_active())

    def _on_btn_auth_click(self, button):
        self._proxy.Authorize('(s)', 'auto')
        button.set_sensitive(False)

    def _on_btn_trust_click(self, button):
        if self.stored:
            self._forget(self.uid)
        else:
            self._trust(self.uid)

    def _on_prop_changes(self, proxy, changed, invalidated):
        # Update current state as properties change
        changed = changed.unpack()
        if 'Stored' in changed:
            self.stored = changed['Stored']
        if 'Status' in changed:
            self.status = changed['Status']
        self._refresh()

class Module:
    name = "thunderbolt"
    category = "hardware"
    comment = _("Manage Thunderbolt™ and USB4 devices")

    def __init__(self, content_box):
        keywords = _("thunderbolt")
        sidePage = SidePage("Thunderbolt™", "csd-thunderbolt", keywords, content_box,
                            module=self)
        self.sidePage = sidePage

    def on_module_selected(self):
        # Check if we've already been loaded
        if self.loaded:
            return

        print("Loading Thunderbolt module")

        # Get the Bolt Manager proxy
        try:
            self.manager_proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.NONE,
                None,
                'org.freedesktop.bolt',
                '/org/freedesktop/bolt',
                'org.freedesktop.bolt1.Manager',
                None
                )
        except GLib.Error:
            self.manager_proxy = None
            print('Cannot acquire org.freedesktop.bolt1.Manager proxy')
            return

        # Subscribe to signals to act on device adds/removals
        self.manager_proxy.connect('g-signal', self._on_manager_proxy_g_signal)

        # Define the settings page
        self.page = SettingsPage()
        self.page.set_spacing(24)
        self.sidePage.add_widget(self.page)

        # Create initial sections for each device
        self._bolt_sections = dict()

        # Initialize known bolt devices
        for obj_path in self.manager_proxy.ListDevices():
            self._build_section(obj_path)

    def _build_section(self, obj_path):
        # Check if we've already built this section
        if obj_path in self._bolt_sections:
            print('Already built section for', obj_path)
            return

        # Get the device proxy
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.NONE,
                None,
                'org.freedesktop.bolt',
                obj_path,
                'org.freedesktop.bolt1.Device',
                None
                )
        except GLib.Error:
            print('Cannot acquire org.freedesktop.bolt1.Device proxy for path', obj_path)
            return

        # Don't build section for host
        if proxy.get_cached_property('Type').unpack() == 'host':
            print('Skipping host type')
            return

        # Init the device and the corresponding section
        bolt_dev = BoltDevice(proxy, self._trust_device, self._forget_device)
        section = self.page.add_section(bolt_dev.vendor + " " + bolt_dev.name)
        widget = SettingsWidget()
        widget.pack_start(bolt_dev.status_label, False, False, 0)
        widget.pack_end(bolt_dev.buttons, False, False, 0)
        section.add_row(widget)
        section.add_reveal_row(bolt_dev.details_box, revealer=bolt_dev.revealer)
        section.show_all()

        # Add to bolt sections we're maintaining
        self._bolt_sections[obj_path] = (section, bolt_dev)

    def _trust_device(self, uid):
        print('Trusting', uid)
        self.manager_proxy.EnrollDevice('(sss)', uid, 'auto', '')

    def _forget_device(self, uid):
        print('Forgetting', uid)
        self.manager_proxy.ForgetDevice('(s)', uid)

    def _on_manager_proxy_g_signal(self, proxy, sender, signal, parameters):
        if signal == 'DeviceAdded':
            (obj_path,) = parameters.unpack()
            self._build_section(obj_path)
        elif signal == 'DeviceRemoved':
            (obj_path,) = parameters.unpack()
            if obj_path in self._bolt_sections:
                self._bolt_sections[obj_path][0].destroy()
                del self._bolt_sections[obj_path]
        

