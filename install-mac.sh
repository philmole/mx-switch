#!/bin/sh
# Start mx-switch at every login on macOS (LaunchAgent).
#   sh install-mac.sh                     -> runs: mx_switch.py --ble --target 1
#   sh install-mac.sh --target 2          -> any arguments replace the defaults
#   sh install-mac.sh --uninstall
# Log: /tmp/mx-switch.log
set -e

LABEL=com.user.mxswitch
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SCRIPT="$(cd "$(dirname "$0")" && pwd)/mx_switch.py"
PYTHON="$(command -v python3)"

launchctl unload "$PLIST" 2>/dev/null || true

if [ "$1" = "--uninstall" ]; then
    rm -f "$PLIST"
    echo "Removed $PLIST"
    exit 0
fi

[ -f "$SCRIPT" ] || { echo "mx_switch.py not found next to this script"; exit 1; }
[ $# -gt 0 ] || set -- --ble --target 1

ARGS=""
for a in "$@"; do
    ARGS="$ARGS        <string>$a</string>
"
done

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT</string>
$ARGS    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/mx-switch.log</string>
    <key>StandardErrorPath</key><string>/tmp/mx-switch.log</string>
</dict>
</plist>
EOF

launchctl load "$PLIST"
echo "Installed: $PYTHON $SCRIPT $*"
echo "Watching /tmp/mx-switch.log (Ctrl+C stops watching, not the script)"
tail -f /tmp/mx-switch.log
