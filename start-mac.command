#!/bin/sh
# Double-click (or add to Login Items) to run mx-switch in a Terminal window on the Mac.
cd "$(dirname "$0")"
exec python3 mx_switch.py --ble --target 1
