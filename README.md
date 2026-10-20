# mx-switch

Press the MX Master 3's **wheel-mode button** (the one in front of the scroll wheel) and both the mouse and the MX Keys jump to the other computer.

- PC (Easy-Switch 1) runs `--target 3`: the button sends both devices to the MacBook.
- MacBook (Easy-Switch 3) runs `--target 1`: the button sends them back to the PC.

The script talks to the devices directly over Bluetooth (Logitech HID++). It does not need Logi Options+. While the script runs, the button no longer toggles ratchet/free-spin, but automatic SmartShift still works. When the script stops, the button goes back to normal.

## Windows (PC)

One-time setup (paths below assume the repo is at `C:\mx-switch`):

```powershell
python -m venv C:\mx-switch\.venv
C:\mx-switch\.venv\Scripts\pip install hidapi
```

To run it manually:

```powershell
C:\mx-switch\.venv\Scripts\python.exe C:\mx-switch\mx_switch.py --target 3
```

To start it automatically at login (no console window), run this once in PowerShell:

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\mx-switch.lnk")
$s.TargetPath = "C:\mx-switch\.venv\Scripts\pythonw.exe"
$s.Arguments = "C:\mx-switch\mx_switch.py --target 3"
$s.Save()
```

## MacBook

No pip install is needed: on macOS the script uses the built-in IOKit through ctypes.

1. Copy `mx_switch.py` to `~/mx-switch/` on the Mac.
2. Check Python with `python3 --version`. If it asks to install the Command Line Tools, accept.
3. First run, from Terminal:
   ```sh
   python3 ~/mx-switch/mx_switch.py --probe
   ```
   If it says `IOHIDDeviceOpen failed ... Input Monitoring`, go to **System Settings → Privacy & Security → Input Monitoring**, turn on **Terminal**, quit Terminal and run it again. You should see both devices, and `00c4 divertable` under the mouse.
4. Test it with `python3 ~/mx-switch/mx_switch.py --target 1`, then press the button.
5. Auto-start at login:
   ```sh
   # edit YOURNAME in the plist first
   cp com.user.mxswitch.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.user.mxswitch.plist
   tail -f /tmp/mx-switch.log
   ```
   Under launchd it's `python3` rather than Terminal that needs Input Monitoring. If the log shows the permission error, add or enable `python3` in that same list. Use the `+` button and press Cmd+Shift+G to reach `/usr/bin/python3` if it isn't listed.

   To stop it: `launchctl unload ~/Library/LaunchAgents/com.user.mxswitch.plist`

### Keeping the Mac awake

The script doesn't touch power settings. If you want the switch back to be instant instead of waiting a few seconds for the Mac to wake:

- **System Settings → Battery → Options → "Prevent automatic sleeping on power adapter when the display is off"**
- Set the display-off and password timings in **System Settings → Lock Screen** to suit you
- Or from Terminal: `sudo pmset -c sleep 0 displaysleep 60` (only applies on the charger)
- Keep **"Allow Bluetooth devices to wake this computer"** on, so switching back wakes a sleeping Mac

## Options

| Flag | |
|---|---|
| `--target 1\|2\|3` | Easy-Switch slot to send the devices to |
| `--probe` | List devices, features and button IDs, then exit. Changes nothing |
| `--debug` | Log every HID++ message |
| `--cid 0x00C3` | Use a different trigger button, e.g. `0x00C3` is the thumb gesture button. Must show as divertable in `--probe` |
| `--match NAME` | Only use devices whose name contains NAME (default `MX`) |

## If something goes wrong

- **Nothing happens on press:** run with `--debug` and press the button. You should see a `<<` line from the mouse. If there's none, another program, such as Logi Options+, may have grabbed the button.
- **Only the mouse switches:** check that the keyboard appears as `connected` in the log before you press.
- **The button stays diverted after a crash:** switch the mouse off and on, or just start the script again.
