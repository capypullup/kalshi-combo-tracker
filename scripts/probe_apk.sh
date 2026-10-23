#!/usr/bin/env bash
# Throwaway probe (CI only): fetch Kalshi's Android app and print the links it declares it opens.
# Reads only the manifest (what Android itself reads to route links); no code is decompiled.
set -u
OUT=/tmp/out; APK=/tmp/apk; mkdir -p "$OUT" "$APK"
PKGS=$(python3 -c "import json;print(' '.join(json.load(open('$OUT/facts.json')).get('packages') or []))" 2>/dev/null)
[ -z "$PKGS" ] && PKGS="com.kalshi.mobile com.kalshi.android com.kalshi.app"
echo "candidate packages: $PKGS"

URL=$(curl -s https://api.github.com/repos/EFForg/apkeep/releases/latest | python3 -c \
  "import json,sys;print(next(a['browser_download_url'] for a in json.load(sys.stdin)['assets'] if a['name'].endswith('x86_64-unknown-linux-gnu')))")
echo "apkeep: $URL"; curl -sL "$URL" -o /tmp/apkeep && chmod +x /tmp/apkeep

PKG=""
for p in $PKGS; do
  echo "== apkeep $p"
  /tmp/apkeep -a "$p" -d apk-pure "$APK" 2>&1 | tail -5
  if ls "$APK" | grep -q "$p"; then PKG=$p; break; fi
  echo "== direct APKPure $p"
  for u in "https://d.apkpure.com/b/XAPK/$p?version=latest" "https://d.apkpure.net/b/XAPK/$p?version=latest" \
           "https://d.apkpure.com/b/APK/$p?version=latest"; do
    curl -sL -A "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36" -o "$APK/$p.xapk" -w "  $u -> %{http_code} %{size_download}B\n" "$u"
    unzip -tq "$APK/$p.xapk" >/dev/null 2>&1 && break
  done
  if unzip -tq "$APK/$p.xapk" >/dev/null 2>&1; then PKG=$p; break; else rm -f "$APK/$p.xapk"; fi
done
ls -la "$APK"
[ -z "$PKG" ] && { echo "NO APK"; exit 0; }
echo "$PKG" > "$OUT/package.txt"

cd "$APK"
F=$(ls | grep "$PKG" | head -1)
mkdir -p x && (unzip -oq "$F" -d x || true)
if ls x/*.apk >/dev/null 2>&1; then echo "split apks:"; ls -la x/*.apk; cp x/*.apk "$APK/"; else cp "$F" "$APK/base.apk"; fi
[ -f x/manifest.json ] && { echo "xapk manifest:"; cat x/manifest.json | head -c 3000; echo; }
BASE=$(ls -S "$APK"/*.apk | head -1); for a in "$APK"/*.apk; do case "$(basename "$a")" in "$PKG.apk"|base.apk) BASE=$a;; esac; done
echo "base apk: $BASE"
AAPT2=$(ls -d "$ANDROID_HOME"/build-tools/*/aapt2 2>/dev/null | sort -V | tail -1)
echo "aapt2: $AAPT2"
"$AAPT2" dump badging "$BASE" 2>/dev/null | grep -E "^package:|sdkVersion|native-code|launchable" | head
echo "== framework hints (file names only)"
for a in "$APK"/*.apk; do unzip -l "$a" | grep -E "libflutter|libapp\.so|index\.android\.bundle|libhermes|libreactnative|flutter_assets/AssetManifest|kotlin/|classes[0-9]*\.dex" | awk '{print $4}' | head -12; done
"$AAPT2" dump xmltree "$BASE" --file AndroidManifest.xml > "$OUT/manifest.txt" 2>&1
echo "manifest lines: $(wc -l < "$OUT/manifest.txt")"
python3 - "$OUT/manifest.txt" <<'PY'
import re, sys
lines = open(sys.argv[1]).read().splitlines()
# Print every <activity>/<activity-alias> that has an intent-filter with a <data> element, with its filters.
cur, out = None, []
for i, l in enumerate(lines):
    if re.search(r"E: (activity|activity-alias) ", l):
        cur = i
    if "E: intent-filter" in l and cur is not None:
        out.append(cur)
seen = set()
for start in out:
    if start in seen:
        continue
    seen.add(start)
    depth = len(lines[start]) - len(lines[start].lstrip())
    block = [lines[start]]
    for l in lines[start + 1:]:
        if l.strip().startswith("E: ") and len(l) - len(l.lstrip()) <= depth:
            break
        block.append(l)
    text = "\n".join(block)
    if "E: data" in text:
        print("\n".join(b for b in block if re.search(r"E: |android:name|scheme|host|path|autoVerify|exported|action|category", b)))
        print("-----")
PY
