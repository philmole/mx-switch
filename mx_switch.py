#!/usr/bin/env python3
"""mx-switch: press the MX Master 3's wheel-mode button (the one in front of the
scroll wheel) to send the mouse AND the MX Keys to another Easy-Switch slot.

Run the same script on each computer:
    PC:      python  mx_switch.py --target 3
    MacBook: python3 mx_switch.py --target 1

Talks Logitech HID++ 2.0 to Bluetooth-connected devices. Windows/Linux use the
`hidapi` pip package; macOS uses IOKit directly via ctypes (no install needed),
because hidapi opens devices exclusively on macOS and would freeze the mouse.
"""
import argparse
import collections
import queue
import signal
import sys
import threading
import time
import traceback

LOGITECH_VID = 0x046D
HIDPP_USAGE_PAGE = 0xFF43  # vendor collection on Bluetooth Logitech devices
REPORT_LONG = 0x11  # BLE devices only accept long (20-byte) HID++ reports
REPORT_LEN = 20
DEVICE_INDEX = 0xFF  # direct Bluetooth connection
SW_ID = 0x0B  # any non-zero value; notifications carry 0

FEAT_ROOT = 0x0000
FEAT_CHANGE_HOST = 0x1814
FEAT_REPROG_V4 = 0x1B04
FEAT_WIRELESS_STATUS = 0x1D4B

DEFAULT_CID = 0x00C4  # MX Master 3 wheel-mode (SmartShift) button

DIVERT_ON = 0x03  # divert + divert-valid
DIVERT_OFF = 0x02  # divert-valid only, i.e. clear the divert

REQUEST_TIMEOUT = 0.5
PROBE_INTERVAL = 0.3  # how often an unreachable device is retried
AWAY_GRACE = 5.0  # after sending a device away, don't write to it for this long
IDLE_PING_INTERVAL = 3.0  # ping a quiet device to notice a manual switch-away
SCAN_FAST = 0.3  # HID enumeration interval while devices are away
SCAN_SLOW = 2.0  # ... and while everything is connected
SWITCH_GAP = 0.15  # keyboard first, then the mouse after this delay
PRESS_DEBOUNCE = 1.0

debug = False
_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(time.strftime("%H:%M:%S"), msg, flush=True)


def dlog(msg):
    if debug:
        log(msg)


def hexs(data):
    return " ".join("%02x" % b for b in data)


# --------------------------------------------------------------------------
# HID backends: enumerate() -> [dict], open_path(path) -> handle with
# read(max_length, timeout_ms) -> list, write(data) -> int, close()
# --------------------------------------------------------------------------

class HidapiBackend(object):
    def __init__(self):
        try:
            import hid
        except ImportError:
            sys.exit("Missing dependency: python -m pip install hidapi")
        self.hid = hid

    def enumerate(self, vid):
        return [
            {
                "path": i["path"],
                "product_id": i["product_id"],
                "product_string": i.get("product_string") or "",
                "usage_page": i.get("usage_page", 0),
            }
            for i in self.hid.enumerate(vid, 0)
        ]

    def open_path(self, path):
        h = self.hid.device()
        h.open_path(path)
        return h


class MacBackend(object):
    """Minimal IOKit HID access via ctypes, opened non-exclusively."""

    kIOReturnNotPermitted = 0xE00002E2
    kCFStringEncodingUTF8 = 0x08000100
    kCFNumberSInt32Type = 3
    kIOHIDReportTypeOutput = 1

    def __init__(self):
        import ctypes
        import ctypes.util

        self.ct = ctypes
        vp = ctypes.c_void_p
        iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))

        def fn(lib, name, res, *args):
            f = getattr(lib, name)
            f.restype = res
            f.argtypes = list(args)
            return f

        self.CALLBACK = ctypes.CFUNCTYPE(
            None, vp, ctypes.c_int, vp, ctypes.c_int, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_long)

        self.ManagerCreate = fn(iokit, "IOHIDManagerCreate", vp, vp, ctypes.c_uint32)
        self.ManagerSetDeviceMatching = fn(iokit, "IOHIDManagerSetDeviceMatching", None, vp, vp)
        self.ManagerCopyDevices = fn(iokit, "IOHIDManagerCopyDevices", vp, vp)
        self.DeviceGetProperty = fn(iokit, "IOHIDDeviceGetProperty", vp, vp, vp)
        self.DeviceGetService = fn(iokit, "IOHIDDeviceGetService", ctypes.c_uint32, vp)
        self.RegistryEntryGetID = fn(iokit, "IORegistryEntryGetRegistryEntryID", ctypes.c_int,
                                     ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64))
        self.DeviceOpen = fn(iokit, "IOHIDDeviceOpen", ctypes.c_int, vp, ctypes.c_uint32)
        self.DeviceClose = fn(iokit, "IOHIDDeviceClose", ctypes.c_int, vp, ctypes.c_uint32)
        self.DeviceSetReport = fn(iokit, "IOHIDDeviceSetReport", ctypes.c_int,
                                  vp, ctypes.c_int, ctypes.c_long, ctypes.c_char_p, ctypes.c_long)
        self.DeviceRegisterInputReportCallback = fn(
            iokit, "IOHIDDeviceRegisterInputReportCallback", None,
            vp, vp, ctypes.c_long, self.CALLBACK, vp)
        self.DeviceScheduleWithRunLoop = fn(iokit, "IOHIDDeviceScheduleWithRunLoop", None, vp, vp, vp)
        self.DeviceUnscheduleFromRunLoop = fn(iokit, "IOHIDDeviceUnscheduleFromRunLoop", None, vp, vp, vp)

        self.CFRetain = fn(cf, "CFRetain", vp, vp)
        self.CFRelease = fn(cf, "CFRelease", None, vp)
        self.CFGetTypeID = fn(cf, "CFGetTypeID", ctypes.c_ulong, vp)
        self.CFStringTypeID = fn(cf, "CFStringGetTypeID", ctypes.c_ulong)()
        self.CFNumberTypeID = fn(cf, "CFNumberGetTypeID", ctypes.c_ulong)()
        self.CFArrayTypeID = fn(cf, "CFArrayGetTypeID", ctypes.c_ulong)()
        self.CFSetGetCount = fn(cf, "CFSetGetCount", ctypes.c_long, vp)
        self.CFSetGetValues = fn(cf, "CFSetGetValues", None, vp, ctypes.POINTER(vp))
        self.CFArrayGetCount = fn(cf, "CFArrayGetCount", ctypes.c_long, vp)
        self.CFArrayGetValueAtIndex = fn(cf, "CFArrayGetValueAtIndex", vp, vp, ctypes.c_long)
        self.CFDictionaryGetValue = fn(cf, "CFDictionaryGetValue", vp, vp, vp)
        self.CFNumberGetValue = fn(cf, "CFNumberGetValue", ctypes.c_bool, vp, ctypes.c_int, vp)
        self.CFStringGetCString = fn(cf, "CFStringGetCString", ctypes.c_bool,
                                     vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32)
        self.CFStringCreate = fn(cf, "CFStringCreateWithCString", vp, vp, ctypes.c_char_p, ctypes.c_uint32)
        self.CFRunLoopGetCurrent = fn(cf, "CFRunLoopGetCurrent", vp)
        self.CFRunLoopRunInMode = fn(cf, "CFRunLoopRunInMode", ctypes.c_int32, vp, ctypes.c_double, ctypes.c_bool)
        self.mode = vp.in_dll(cf, "kCFRunLoopDefaultMode")
        self._keys = {}

    def _key(self, s):
        if s not in self._keys:
            self._keys[s] = self.CFStringCreate(None, s.encode(), self.kCFStringEncodingUTF8)
        return self._keys[s]

    def _num(self, ref):
        if not ref or self.CFGetTypeID(ref) != self.CFNumberTypeID:
            return None
        out = self.ct.c_int32()
        self.CFNumberGetValue(ref, self.kCFNumberSInt32Type, self.ct.byref(out))
        return out.value

    def _prop_num(self, dev, key):
        return self._num(self.DeviceGetProperty(dev, self._key(key)))

    def _prop_str(self, dev, key):
        ref = self.DeviceGetProperty(dev, self._key(key))
        if not ref or self.CFGetTypeID(ref) != self.CFStringTypeID:
            return ""
        buf = self.ct.create_string_buffer(256)
        self.CFStringGetCString(ref, buf, 256, self.kCFStringEncodingUTF8)
        return buf.value.decode("utf-8", "replace")

    def _usage_pages(self, dev):
        arr = self.DeviceGetProperty(dev, self._key("DeviceUsagePairs"))
        if not arr or self.CFGetTypeID(arr) != self.CFArrayTypeID:
            return []
        pages = []
        for i in range(self.CFArrayGetCount(arr)):
            d = self.CFArrayGetValueAtIndex(arr, i)
            page = self._num(self.CFDictionaryGetValue(d, self._key("DeviceUsagePage")))
            if page is not None:
                pages.append(page)
        return pages

    def _path(self, dev):
        regid = self.ct.c_uint64()
        self.RegistryEntryGetID(self.DeviceGetService(dev), self.ct.byref(regid))
        return b"DevSrvsID:%d" % regid.value

    def _devices(self, vid, visit):
        mgr = self.ManagerCreate(None, 0)
        try:
            self.ManagerSetDeviceMatching(mgr, None)
            devset = self.ManagerCopyDevices(mgr)
            if not devset:
                return
            try:
                n = self.CFSetGetCount(devset)
                arr = (self.ct.c_void_p * n)()
                self.CFSetGetValues(devset, arr)
                for dev in arr:
                    if self._prop_num(dev, "VendorID") == vid and visit(dev) is False:
                        break
            finally:
                self.CFRelease(devset)
        finally:
            self.CFRelease(mgr)

    def enumerate(self, vid):
        out = []

        def visit(dev):
            pages = self._usage_pages(dev)
            out.append({
                "path": self._path(dev),
                "product_id": self._prop_num(dev, "ProductID") or 0,
                "product_string": self._prop_str(dev, "Product"),
                # One IOHIDDevice covers all collections; report the HID++ page if present.
                "usage_page": HIDPP_USAGE_PAGE if HIDPP_USAGE_PAGE in pages else (pages[0] if pages else 0),
            })

        self._devices(vid, visit)
        return out

    def open_path(self, path):
        found = []

        def visit(dev):
            if self._path(dev) == path:
                found.append(self.CFRetain(dev))
                return False

        self._devices(LOGITECH_VID, visit)
        if not found:
            raise IOError("device not found")
        return MacHandle(self, found[0])


class MacHandle(object):
    """Must be read from the thread that opened it (reports arrive on its run loop)."""

    def __init__(self, mac, dev):
        self.mac = mac
        self.dev = dev
        ret = mac.DeviceOpen(dev, 0) & 0xFFFFFFFF  # 0 = shared, not seized
        if ret:
            mac.CFRelease(dev)
            hint = ""
            if ret == MacBackend.kIOReturnNotPermitted:
                hint = (" - grant Input Monitoring to Terminal/python3 in System Settings > Privacy & Security,"
                        " or run with --ble")
            raise IOError("IOHIDDeviceOpen failed 0x%08x%s" % (ret, hint))
        self.reports = collections.deque()
        self.buf = mac.ct.create_string_buffer(64)
        self.cb = mac.CALLBACK(self._on_report)  # keep a reference so it isn't collected
        mac.DeviceRegisterInputReportCallback(dev, self.buf, 64, self.cb, None)
        self.runloop = mac.CFRunLoopGetCurrent()
        mac.DeviceScheduleWithRunLoop(dev, self.runloop, mac.mode)

    def _on_report(self, ctx, result, sender, rtype, report_id, report, length):
        self.reports.append(bytes(bytearray(report[:length])))

    def read(self, max_length, timeout_ms):
        if not self.reports:
            self.mac.CFRunLoopRunInMode(self.mac.mode, timeout_ms / 1000.0, True)
        return list(self.reports.popleft()[:max_length]) if self.reports else []

    def write(self, data):
        buf = bytes(bytearray(data))
        ret = self.mac.DeviceSetReport(self.dev, MacBackend.kIOHIDReportTypeOutput, buf[0], buf, len(buf))
        return len(buf) if ret == 0 else -1

    def close(self):
        if self.dev:
            self.mac.DeviceUnscheduleFromRunLoop(self.dev, self.runloop, self.mac.mode)
            self.mac.DeviceClose(self.dev, 0)
            self.mac.CFRelease(self.dev)
            self.dev = None


GATT_LOGI_SUFFIX = "011F2000046D"  # Logitech's vendor GATT service/characteristic UUIDs end with this
GATT_PINNED = "==10.3.1"  # last pyobjc with wheels for macOS's built-in Python 3.9


def _gatt_delegate_class():
    """Defined lazily so pyobjc is only imported when --ble is used."""
    import objc
    from Foundation import NSObject

    class MxSwitchGattDelegate(NSObject):
        """CoreBluetooth callbacks, forwarded to the GattHandle of each peripheral."""

        @objc.python_method
        def _h(self, p):
            return self.owner.handles.get(p.identifier().UUIDString())

        def centralManagerDidUpdateState_(self, central):
            if central.state() >= 2:  # past Unknown/Resetting
                self.owner.state_known.set()

        def centralManager_didConnectPeripheral_(self, central, p):
            p.discoverServices_(None)

        def centralManager_didFailToConnectPeripheral_error_(self, central, p, err):
            h = self._h(p)
            if h:
                h.fail("connect failed: %s" % err)

        def centralManager_didDisconnectPeripheral_error_(self, central, p, err):
            h = self._h(p)
            if h:
                h.fail("disconnected")

        def peripheral_didDiscoverServices_(self, p, err):
            h = self._h(p)
            if h:
                h.on_services(err)

        def peripheral_didDiscoverCharacteristicsForService_error_(self, p, service, err):
            h = self._h(p)
            if h:
                h.on_characteristics(service, err)

        def peripheral_didUpdateNotificationStateForCharacteristic_error_(self, p, c, err):
            h = self._h(p)
            if h:
                h.on_notify_state(err)

        def peripheral_didUpdateValueForCharacteristic_error_(self, p, c, err):
            h = self._h(p)
            if h and not err and c.value() is not None:
                h.on_value(bytes(c.value()))

    return MxSwitchGattDelegate


class GattBackend(object):
    """macOS without Input Monitoring: HID++ over Logitech's vendor GATT service
    through CoreBluetooth, which only needs the Bluetooth permission."""

    def __init__(self):
        try:
            import CoreBluetooth
            import Foundation
            import libdispatch
        except ImportError:
            sys.exit("Missing dependency: python3 -m pip install --user "
                     "pyobjc-framework-CoreBluetooth%s pyobjc-framework-libdispatch%s" % (GATT_PINNED, GATT_PINNED))
        self.CB = CoreBluetooth
        self.NSData = Foundation.NSData
        self.handles = {}  # peripheral UUID -> GattHandle
        self.state_known = threading.Event()
        self.delegate = _gatt_delegate_class().alloc().init()
        self.delegate.owner = self
        dq = libdispatch.dispatch_queue_create(b"mx-switch.ble", None)  # callbacks run off the main thread
        self.central = CoreBluetooth.CBCentralManager.alloc().initWithDelegate_queue_(self.delegate, dq)
        self.state_known.wait(5)
        if self.central.state() != CoreBluetooth.CBManagerStatePoweredOn:
            sys.exit("Bluetooth unavailable (state %d) - is it on, and allowed for Terminal/python3 under "
                     "System Settings > Privacy & Security > Bluetooth?" % self.central.state())

    def _connected(self):
        uuids = [self.CB.CBUUID.UUIDWithString_(u) for u in ("1812", "180A", "180F")]
        return self.central.retrieveConnectedPeripheralsWithServices_(uuids) or []

    def enumerate(self, vid):
        return [
            {
                "path": p.identifier().UUIDString(),
                "product_id": 0,
                "product_string": p.name() or "",
                "usage_page": HIDPP_USAGE_PAGE,  # checked for real when it's opened
            }
            for p in self._connected()
        ]

    def open_path(self, path):
        for p in self._connected():
            if p.identifier().UUIDString() == path:
                return GattHandle(self, p)
        raise IOError("device not found")


class GattHandle(object):
    """Takes and returns the same 20-byte HID++ reports as the HID handles. The
    characteristic may carry them without the report ID and device index; which
    framing the device uses is detected on open."""

    def __init__(self, backend, peripheral):
        self.b = backend
        self.p = peripheral
        self.key = peripheral.identifier().UUIDString()
        self.rx = self.tx = None
        self.error = None
        self.setup_done = threading.Event()
        self.reports = queue.Queue()
        self.closed = False
        self.full = False  # True if the characteristic carries complete 20-byte reports
        self.last_write = 0.0
        backend.handles[self.key] = self
        peripheral.setDelegate_(backend.delegate)
        backend.central.connectPeripheral_options_(peripheral, None)
        if not self.setup_done.wait(5) or self.error or self.rx is None or self.tx is None:
            err = self.error or "timed out"
            self.close()
            raise IOError("GATT setup failed: %s" % err)
        self._detect_framing()

    # -- CoreBluetooth callbacks (dispatch queue thread) --------------------

    def fail(self, msg):
        self.error = self.error or msg
        self.setup_done.set()
        self.reports.put(None)

    def on_services(self, err):
        services = [s for s in (self.p.services() or [])
                    if s.UUID().UUIDString().upper().endswith(GATT_LOGI_SUFFIX)]
        if err or not services:
            self.fail("no Logitech vendor service (%s)" % err)
            return
        self.p.discoverCharacteristics_forService_(None, services[0])

    def on_characteristics(self, service, err):
        if err:
            self.fail("characteristic discovery failed: %s" % err)
            return
        chars = service.characteristics() or []
        # Prefer one characteristic that does both; otherwise split notify / write.
        both = [c for c in chars if c.properties() & 0x10 and c.properties() & 0x0C]
        self.rx = both[0] if both else next((c for c in chars if c.properties() & 0x10), None)
        self.tx = both[0] if both else next((c for c in chars if c.properties() & 0x0C), None)
        dlog("%s GATT chars %s" % (self.p.name(), ", ".join(
            "%s/%02x" % (c.UUID().UUIDString(), c.properties()) for c in chars)))
        if self.rx is None or self.tx is None:
            self.fail("no usable HID++ characteristic")
            return
        self.p.setNotifyValue_forCharacteristic_(True, self.rx)

    def on_notify_state(self, err):
        if err:
            self.fail("enabling notifications failed: %s" % err)
        else:
            self.setup_done.set()

    def on_value(self, data):
        if not (len(data) >= REPORT_LEN and data[0] in (0x10, 0x11) and data[1] == DEVICE_INDEX):
            data = bytes([REPORT_LONG, DEVICE_INDEX]) + data
        self.reports.put(data)

    # -- handle interface (device thread) -----------------------------------

    def _detect_framing(self):
        ping = [REPORT_LONG, DEVICE_INDEX, 0x00, 0x10 | SW_ID, 0, 0, 0x5A]  # Root.getProtocolVersion
        ping += [0] * (REPORT_LEN - len(ping))
        for full in (False, True):
            self.full = full
            self.write(ping)
            deadline = time.time() + REQUEST_TIMEOUT
            while time.time() < deadline:
                data = self.read(64, 100)
                if len(data) >= 7 and data[2] == 0x00 and data[3] == 0x10 | SW_ID and data[6] == 0x5A:
                    dlog("%s GATT framing: %s" % (self.p.name(), "20-byte" if full else "18-byte"))
                    return
        self.close()
        raise IOError("no HID++ reply over GATT")

    def read(self, max_length, timeout_ms):
        if self.closed:
            raise IOError("closed")
        try:
            data = self.reports.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return []
        if data is None:
            raise IOError(self.error or "disconnected")
        return list(data[:max_length])

    def write(self, data):
        if self.closed or self.error:
            return -1
        buf = bytes(bytearray(data))
        if not self.full:
            buf = buf[2:]
        with_response = self.tx.properties() & 0x08
        self.p.writeValue_forCharacteristic_type_(
            self.b.NSData.dataWithBytes_length_(buf, len(buf)), self.tx,
            self.b.CB.CBCharacteristicWriteWithResponse if with_response
            else self.b.CB.CBCharacteristicWriteWithoutResponse)
        self.last_write = time.time()
        return len(buf)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if time.time() - self.last_write < 0.3:
            time.sleep(0.3)  # writes are async; let e.g. the button release go out first
        self.b.handles.pop(self.key, None)
        self.b.central.cancelPeripheralConnection_(self.p)  # drops our link only, not the system's
        self.reports.put(None)


backend = None


# --------------------------------------------------------------------------
# HID++ device
# --------------------------------------------------------------------------

class HidppError(Exception):
    pass


class Device(object):
    """One Bluetooth HID++ device, driven by its own thread."""

    def __init__(self, manager, info):
        self.manager = manager
        self.path = info["path"]
        self.name = info["product_string"] or "PID %04x" % info["product_id"]
        self.handle = None
        self.write_lock = threading.Lock()
        self.features = {}
        self.has_button = False
        self.pressed = False
        self.needs_setup = False
        self.ready = False
        self.away = threading.Event()  # set once we've sent it to another host
        self.away_since = 0.0
        self.switch_to = None  # host index queued for this device's own thread
        self.switched = threading.Event()
        self.stop = False
        self.thread = threading.Thread(target=self.run, name=self.name)
        self.thread.daemon = True

    # -- low level ---------------------------------------------------------

    def open(self):
        self.handle = backend.open_path(self.path)

    def close(self):
        self.ready = False
        if self.handle is not None:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None

    def write(self, feat_index, function, params=b""):
        report = bytes([REPORT_LONG, DEVICE_INDEX, feat_index, (function << 4) | SW_ID]) + bytes(params)
        report = report.ljust(REPORT_LEN, b"\0")
        dlog("%s >> %s" % (self.name, hexs(report)))
        with self.write_lock:
            n = self.handle.write(list(report))
        if n is not None and n < 0:
            raise IOError("write failed")

    def read(self, timeout):
        data = self.handle.read(64, max(1, int(timeout * 1000)))
        if data:
            data = bytes(bytearray(data))
            dlog("%s << %s" % (self.name, hexs(data)))
            return data
        return b""

    def request(self, feat_index, function, params=b""):
        self.write(feat_index, function, params)
        func_sw = (function << 4) | SW_ID
        deadline = time.time() + REQUEST_TIMEOUT
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise HidppError("timeout")
            data = self.read(remaining)
            if len(data) < 6 or data[0] not in (0x10, 0x11):
                continue
            if data[2] in (0xFF, 0x8F) and data[3] == feat_index and data[4] == func_sw:
                raise HidppError("HID++ error %d" % data[5])
            if data[2] == feat_index and data[3] == func_sw:
                return data[4:]
            if self.ready:
                self.handle_notification(data)

    def ping(self):
        r = self.request(0, 1, b"\x00\x00\x5a")  # Root.getProtocolVersion with ping byte
        return r[0], r[1]

    def feature_index(self, feature_id):
        r = self.request(0, 0, bytes([feature_id >> 8, feature_id & 0xFF]))  # Root.getFeature
        return r[0] or None

    def set_divert(self, on):
        cid = self.manager.cid
        self.request(self.features[FEAT_REPROG_V4], 3,
                     bytes([cid >> 8, cid & 0xFF, DIVERT_ON if on else DIVERT_OFF, 0, 0]))

    # -- behaviour ---------------------------------------------------------

    def setup(self):
        self.ping()  # raises if the device is connected elsewhere
        self.features = {}
        for fid in (FEAT_CHANGE_HOST, FEAT_REPROG_V4, FEAT_WIRELESS_STATUS):
            idx = self.feature_index(fid)
            if idx:
                self.features[fid] = idx
        dlog("%s features %s" % (self.name, {"%04x" % k: v for k, v in self.features.items()}))
        self.has_button = False
        if FEAT_REPROG_V4 in self.features:
            cid = self.manager.cid
            try:
                self.request(self.features[FEAT_REPROG_V4], 2, bytes([cid >> 8, cid & 0xFF]))  # getCidReporting
                self.has_button = True
            except HidppError:
                pass  # this device doesn't have that control (e.g. the keyboard)
            if self.has_button:
                self.set_divert(True)
        self.pressed = False
        self.needs_setup = False
        host = ""
        if FEAT_CHANGE_HOST in self.features:
            r = self.request(self.features[FEAT_CHANGE_HOST], 0)  # getHostInfo
            host = " on Easy-Switch %d of %d" % (r[1] + 1, r[0])
            if r[1] == self.manager.host_index:
                log("WARNING: --target %d is the slot this computer already uses" % (r[1] + 1))
        log("%s connected%s%s" % (self.name, host, " - button active" if self.has_button else ""))

    def run(self):
        try:
            self._run()
        except Exception:
            log("%s: unexpected error\n%s" % (self.name, traceback.format_exc()))
            time.sleep(5)
        finally:
            self.release_button()
            self.close()

    def _run(self):
        was_connected = False
        while not self.stop and self.manager.running:
            if self.away.is_set():
                self.wait_while_away()
                continue
            try:
                if self.handle is None:
                    self.open()
                self.setup()
            except HidppError as e:
                dlog("%s not reachable: %s" % (self.name, e))
                time.sleep(PROBE_INTERVAL)
                continue
            except (IOError, OSError, ValueError) as e:
                dlog("%s open/setup failed: %s" % (self.name, e))
                self.close()
                time.sleep(PROBE_INTERVAL)
                continue

            self.ready = True
            was_connected = True
            try:
                self.listen()
            except HidppError as e:
                dlog("%s stopped responding: %s" % (self.name, e))
            except (IOError, OSError, ValueError) as e:
                dlog("%s I/O error: %s" % (self.name, e))
                self.close()
            self.ready = False
            if not self.needs_setup and not self.stop and self.manager.running:
                log("%s disconnected" % self.name)
        if was_connected and self.manager.running:
            dlog("%s gone" % self.name)

    def wait_while_away(self):
        """Stay silent for a few seconds after sending the device away: on Windows
        any write makes the PC reconnect it, and mid-switch that can win the race
        and pull it back to this slot. Ends early if the device speaks first
        (it sends a Wireless Device Status event when it reconnects)."""
        deadline = self.away_since + AWAY_GRACE
        while time.time() < deadline and not self.stop and self.manager.running:
            if self.handle is None:
                time.sleep(0.1)
                continue
            try:
                data = self.read(0.1)
            except (IOError, OSError, ValueError):
                self.close()
                continue
            # Only a reconnect event counts; stray reports (e.g. the button
            # release) can still trickle in just after the switch.
            if (len(data) >= 5 and data[2] == self.features.get(FEAT_WIRELESS_STATUS)
                    and data[3] == 0x00 and data[4] == 1):
                dlog("%s is back early" % self.name)
                break
        self.away.clear()

    def listen(self):
        last_seen = time.time()
        while not self.stop and self.manager.running:
            if self.switch_to is not None:
                host, self.switch_to = self.switch_to, None
                self.change_host(host)
                self.switched.set()
            if self.away.is_set() or self.needs_setup:
                return
            data = self.read(0.1)
            if data:
                last_seen = time.time()
                self.handle_notification(data)
            elif time.time() - last_seen > IDLE_PING_INTERVAL:
                self.ping()
                last_seen = time.time()

    def handle_notification(self, data):
        if len(data) < 5 or data[0] not in (0x10, 0x11) or data[3] & 0x0F:
            return  # not a notification (responses carry our SW_ID)
        feat, func = data[2], data[3] >> 4
        if self.has_button and feat == self.features.get(FEAT_REPROG_V4) and func == 0:
            params = data[4:12].ljust(8, b"\0")
            cids = [(params[i] << 8) | params[i + 1] for i in range(0, 8, 2)]
            down = self.manager.cid in cids
            if down and not self.pressed:
                self.manager.on_press(self)
            self.pressed = down
        elif feat == self.features.get(FEAT_WIRELESS_STATUS) and func == 0 and data[4] == 1:
            dlog("%s asked for reconfiguration" % self.name)
            self.needs_setup = True

    def request_switch(self, host_index):
        """Queue a switch for this device's own thread: hidapi handles aren't
        safe to write from one thread while another is reading them."""
        if not self.ready or FEAT_CHANGE_HOST not in self.features:
            return False
        self.switched.clear()
        self.switch_to = host_index
        return True

    def change_host(self, host_index):
        """Must run on this device's own thread."""
        idx = self.features.get(FEAT_CHANGE_HOST)
        if not self.ready or idx is None or self.handle is None:
            return False
        try:
            self.write(idx, 1, bytes([host_index]))  # setCurrentHost; no reply, it just leaves
        except (IOError, OSError, ValueError) as e:
            log("%s: switch failed: %s" % (self.name, e))
            return False
        log("%s -> Easy-Switch %d" % (self.name, host_index + 1))
        self.away_since = time.time()
        self.away.set()
        return True

    def release_button(self):
        """Give the wheel-mode button back its normal function. Runs on this
        device's own thread as it exits."""
        if self.has_button and self.handle is not None and not self.away.is_set():
            cid = self.manager.cid
            try:
                self.write(self.features[FEAT_REPROG_V4], 3, bytes([cid >> 8, cid & 0xFF, DIVERT_OFF, 0, 0]))
            except Exception:
                pass


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------

def hidpp_devices(match):
    out = []
    for info in backend.enumerate(LOGITECH_VID):
        if info["usage_page"] != HIDPP_USAGE_PAGE:
            continue
        name = info["product_string"].lower()
        if name and match and not any(m in name for m in match):
            continue
        out.append(info)
    return out


class Manager(object):
    def __init__(self, target, cid, match):
        self.host_index = target - 1
        self.cid = cid
        self.match = match
        self.devices = {}  # path -> Device
        self.lock = threading.Lock()
        self.running = True
        self.last_press = 0.0

    def scan(self):
        found = {info["path"]: info for info in hidpp_devices(self.match)}
        with self.lock:
            for path, dev in list(self.devices.items()):
                if not dev.thread.is_alive():
                    del self.devices[path]
                elif path not in found:
                    dev.stop = True
            for path, info in found.items():
                if path not in self.devices:
                    dev = Device(self, info)
                    self.devices[path] = dev
                    dlog("found %s" % dev.name)
                    dev.thread.start()

    def all_ready(self):
        with self.lock:
            devs = list(self.devices.values())
        return bool(devs) and all(d.ready for d in devs)

    def on_press(self, source):
        now = time.time()
        if now - self.last_press < PRESS_DEBOUNCE:
            return
        self.last_press = now
        log("Button pressed - switching to Easy-Switch %d" % (self.host_index + 1))
        with self.lock:
            others = [d for d in self.devices.values() if d is not source]
        queued = []
        for d in others:
            if d.request_switch(self.host_index):
                queued.append(d)
            else:
                log("%s not connected - skipped" % d.name)
        for d in queued:
            if not d.switched.wait(0.5):
                log("%s didn't switch in time" % d.name)
        if queued:
            time.sleep(SWITCH_GAP)
        source.change_host(self.host_index)  # on_press runs on the source's own thread

    def loop(self):
        while True:
            self.scan()
            time.sleep(SCAN_SLOW if self.all_ready() else SCAN_FAST)

    def shutdown(self):
        self.running = False  # each device thread restores its button as it exits
        with self.lock:
            devs = list(self.devices.values())
        for d in devs:
            d.thread.join(1.5)


def probe(match, cid):
    """List devices and what they support, without changing anything."""
    print("Logitech HID devices:")
    for info in backend.enumerate(LOGITECH_VID):
        print("  PID %04x  usage page %04x  %s" % (info["product_id"], info["usage_page"], info["product_string"]))

    class Probe(object):
        pass

    mgr = Probe()
    mgr.cid = cid
    for info in hidpp_devices(match):
        dev = Device(mgr, info)
        print("\n%s" % dev.name)
        try:
            dev.open()
            major, minor = dev.ping()
            print("  HID++ %d.%d" % (major, minor))
            for fid in (FEAT_CHANGE_HOST, FEAT_REPROG_V4, FEAT_WIRELESS_STATUS):
                print("  feature %04x -> index %s" % (fid, dev.feature_index(fid)))
            ch = dev.feature_index(FEAT_CHANGE_HOST)
            if ch:
                r = dev.request(ch, 0)
                print("  on Easy-Switch %d of %d" % (r[1] + 1, r[0]))
            rk = dev.feature_index(FEAT_REPROG_V4)
            if rk:
                count = dev.request(rk, 0)[0]
                print("  controls:")
                for i in range(count):
                    r = dev.request(rk, 1, bytes([i]))
                    c = (r[0] << 8) | r[1]
                    flags = r[4] | (r[8] << 8)
                    print("    CID %04x%s%s" % (c, "  divertable" if flags & 0x20 else "",
                                              "  <- --cid" if c == cid else ""))
        except (HidppError, IOError, OSError, ValueError) as e:
            print("  not reachable: %s (connected to another computer?)" % e)
        finally:
            dev.close()


def main():
    global backend, debug
    p = argparse.ArgumentParser(description="Wheel-mode button sends MX mouse + keyboard to another Easy-Switch slot.")
    p.add_argument("--target", type=int, choices=(1, 2, 3), help="Easy-Switch slot to send the devices to")
    p.add_argument("--cid", type=lambda s: int(s, 0), default=DEFAULT_CID,
                   help="control ID of the trigger button (default 0x00C4, wheel-mode button)")
    p.add_argument("--match", action="append",
                   help="only use devices whose name contains this (default: MX); repeatable")
    p.add_argument("--probe", action="store_true", help="list devices, features and button CIDs, then exit")
    p.add_argument("--debug", action="store_true", help="log all HID++ traffic")
    p.add_argument("--ble", action="store_true",
                   help="macOS: talk over Bluetooth GATT (needs the Bluetooth permission, not Input Monitoring)")
    args = p.parse_args()

    debug = args.debug
    if sys.platform == "darwin":
        backend = GattBackend() if args.ble else MacBackend()
    else:
        backend = HidapiBackend()
    match = [m.lower() for m in (args.match or ["MX"])]

    if args.probe:
        probe(match, args.cid)
        return
    if not args.target:
        p.error("--target is required (or use --probe)")

    mgr = Manager(args.target, args.cid, match)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    log("mx-switch: wheel-mode button sends devices to Easy-Switch %d. Ctrl+C to quit." % args.target)
    try:
        mgr.loop()
    except KeyboardInterrupt:
        pass
    finally:
        mgr.shutdown()
        log("stopped - button restored")


if __name__ == "__main__":
    main()
