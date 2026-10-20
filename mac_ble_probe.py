#!/usr/bin/env python3
"""Probe: can macOS reach the MX devices' HID++ over Bluetooth LE GATT instead of
IOKit HID? GATT access needs the Bluetooth permission, not Input Monitoring.

Setup (no admin needed):  pip3 install --user pyobjc-framework-CoreBluetooth
Run:                      python3 mac_ble_probe.py

Changes nothing. Lists the GATT services and characteristics of connected MX devices.
"""
import time

import objc
from Foundation import NSDate, NSObject, NSRunLoop
import CoreBluetooth as CB

TIMEOUT = 20
LOGI_SUFFIX = "011F2000046D"  # Logitech vendor GATT UUIDs end with this


class Probe(NSObject):
    def init(self):
        self = objc.super(Probe, self).init()
        self.periphs = []
        self.pending = {}  # peripheral id -> services left to read
        self.vendor_hits = []
        self.done = False
        self.mgr = CB.CBCentralManager.alloc().initWithDelegate_queue_(self, None)
        return self

    def centralManagerDidUpdateState_(self, mgr):
        state = mgr.state()
        if state == CB.CBManagerStateUnauthorized:
            print("Bluetooth access denied: allow Terminal under Privacy & Security > Bluetooth")
            self.done = True
        elif state in (CB.CBManagerStateUnsupported, CB.CBManagerStatePoweredOff):
            print("Bluetooth is off or unsupported (state %d)" % state)
            self.done = True
        elif state == CB.CBManagerStatePoweredOn:
            uuids = [CB.CBUUID.UUIDWithString_(u) for u in ("1812", "180A", "180F")]
            for p in mgr.retrieveConnectedPeripheralsWithServices_(uuids):
                name = p.name() or ""
                print("connected: %s" % name)
                if "MX" not in name:
                    continue
                p.setDelegate_(self)
                self.periphs.append(p)  # keep a reference or CoreBluetooth drops it
                self.pending[p.identifier().UUIDString()] = None
                mgr.connectPeripheral_options_(p, None)
            if not self.periphs:
                print("No connected MX devices found over GATT")
                self.done = True

    def centralManager_didConnectPeripheral_(self, mgr, p):
        p.discoverServices_(None)

    def centralManager_didFailToConnectPeripheral_error_(self, mgr, p, err):
        print("%s: connect failed: %s" % (p.name(), err))
        self._finish(p)

    def peripheral_didDiscoverServices_(self, p, err):
        services = p.services() or []
        if err or not services:
            print("%s: no services (%s)" % (p.name(), err))
            self._finish(p)
            return
        self.pending[p.identifier().UUIDString()] = len(services)
        for s in services:
            p.discoverCharacteristics_forService_(None, s)

    def peripheral_didDiscoverCharacteristicsForService_error_(self, p, s, err):
        suuid = s.UUID().UUIDString()
        lines = ["%s  service %s" % (p.name(), suuid)]
        for c in s.characteristics() or []:
            lines.append("    char %s  props 0x%02x" % (c.UUID().UUIDString(), c.properties()))
        if suuid.upper().endswith(LOGI_SUFFIX):
            self.vendor_hits.append("%s: %s" % (p.name(), suuid))
        print("\n".join(lines))
        key = p.identifier().UUIDString()
        self.pending[key] -= 1
        if self.pending[key] <= 0:
            self._finish(p)

    @objc.python_method
    def _finish(self, p):
        self.pending.pop(p.identifier().UUIDString(), None)
        if not self.pending:
            self.done = True


def main():
    probe = Probe.alloc().init()
    end = time.time() + TIMEOUT
    while not probe.done and time.time() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.2))
    if not probe.done:
        print("Timed out")
    print()
    if probe.vendor_hits:
        print("Logitech vendor service found - HID++ over GATT looks possible:")
        print("\n".join("  " + h for h in probe.vendor_hits))
    else:
        print("No Logitech vendor GATT service seen - this route probably won't work")


if __name__ == "__main__":
    main()
