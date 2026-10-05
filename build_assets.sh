#!/usr/bin/env bash
# Rebuild static/dist/app.min.{css,js} after editing static/css/*.css or static/js/*.js (needs Node: npx esbuild).
set -e
cd "$(dirname "$0")"
FONTS='@font-face{font-family:Inter;font-style:normal;font-weight:400 800;font-display:swap;src:url(../webfonts/inter-latin-wght-normal.woff2) format("woff2")}
@font-face{font-family:Caveat;font-style:normal;font-weight:600;font-display:swap;src:url(../webfonts/caveat-latin-600-normal.woff2) format("woff2")}
@font-face{font-family:"Inter Fallback";src:local("Arial");size-adjust:107%;ascent-override:90%;descent-override:22%;line-gap-override:0%}
i[data-lucide]{display:inline-block;width:24px;height:24px;flex:none}'
mkdir -p static/dist
{ echo "$FONTS"; cat static/css/style.css static/css/own_handwriting.css; } | sed 's/font-family:Inter,system-ui,sans-serif/font-family:Inter,"Inter Fallback",system-ui,sans-serif/' > /tmp/all.css
npx --yes esbuild /tmp/all.css --minify --outfile=static/dist/app.min.css
for f in icons script own_handwriting; do npx --yes esbuild static/js/$f.js --minify --outfile=/tmp/$f.min.js; done
cat /tmp/icons.min.js /tmp/script.min.js /tmp/own_handwriting.min.js > static/dist/app.min.js
