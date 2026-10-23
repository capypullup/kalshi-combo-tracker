#!/usr/bin/env bash
# Throwaway probe (CI only): open candidate links in Kalshi's Android app on an emulator and
# screenshot where each one lands. Black-box: it only fires the same intents a tapped link would.
set -u
O=/tmp/emu; mkdir -p "$O"; A=/tmp/apk
PKG=$(cat /tmp/out/package.txt 2>/dev/null)
[ -z "$PKG" ] && { echo "no package"; exit 0; }
T=$(python3 -c "import json;print(json.load(open('/tmp/out/facts.json'))['combo'])")
L=$(python3 -c "import json;print(json.load(open('/tmp/out/facts.json'))['leg'])")
E=$(python3 -c "import json;print(json.load(open('/tmp/out/facts.json'))['leg_event'])")
adb wait-for-device; adb shell getprop ro.product.cpu.abilist
adb shell settings put global window_animation_scale 0; adb shell settings put global transition_animation_scale 0
echo "== install"; adb install-multiple -r -g $(ls "$A"/*.apk | grep -v "/$PKG.xapk") 2>&1 | tail -3
adb shell pm list packages | grep -i kalshi
echo "== app links"; adb shell pm get-app-links "$PKG" 2>&1 | head -30
shot() {
  adb shell screencap -p /sdcard/s.png && adb pull -q /sdcard/s.png "$O/$1.png"
  timeout 25 adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1 && adb pull -q /sdcard/ui.xml "$O/$1.xml"
  echo "-- $1 top: $(adb shell dumpsys activity activities | grep -m1 -E 'topResumedActivity|mResumedActivity')"
  [ -f "$O/$1.xml" ] && python3 -c "
import re,sys,html
x=open('$O/$1.xml',encoding='utf-8',errors='ignore').read()
t=[html.unescape(s) for s in re.findall(r'(?:text|content-desc)=\"([^\"]+)\"',x)]
print('   ui text:', ' | '.join(t)[:1500])"
}
echo "== cold start"; adb logcat -c
adb shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; sleep 30; shot 00-launch
i=1
for U in "https://kalshi.com/markets_by_ticker/$T" "https://kalshi.com/combos?marketTicker=$T" \
         "https://kalshi.com/markets_by_ticker/$L" "https://kalshi.com/events/$E" "https://kalshi.com/combos"; do
  echo "== link $i: $U"
  adb shell am start -W -a android.intent.action.VIEW -d "\"$U\"" "$PKG" 2>&1 | head -8
  sleep 15; shot "0$i-link"; i=$((i+1))
done
echo "== resolver without a package for the combo link"
adb shell cmd package query-activities -a android.intent.action.VIEW -d "https://kalshi.com/markets_by_ticker/$T" 2>&1 | grep -E "packageName|name=" | head
adb shell cmd package query-activities -a android.intent.action.VIEW -d "https://kalshi.com/combos?marketTicker=$T" 2>&1 | grep -E "packageName|name=" | head
echo "== app log"; adb logcat -d 2>/dev/null | grep -iE "flutter|kalshi|deep ?link|route|uri" | grep -viE "chatty|GoogleApiManager" | tail -150
