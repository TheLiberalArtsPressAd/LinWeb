#!/usr/bin/env python3
"""Patch the exported Godot ``index.html`` into the GitHub Pages shell.

The exporter emits a plain loader that expects uncompressed ``index.pck`` /
``index.wasm`` next to it. The Pages deployment instead ships those two files
gzip-compressed (the published site must stay under the 1 GB Pages cap) and
needs a cross-origin-isolated document (the .NET WebAssembly runtime transfers
SharedArrayBuffers to its worker pool, and static hosts cannot send COOP/COEP
headers — ``stream-cache-sw.js`` injects them).

Keeping this in a script instead of hand-editing the deployed file means a
fresh export can be republished with one command and cannot drift.

Usage:
    python tools/patch_index.py --build-dir ../../IdleLineage_Source_Restored/build/web_csharp

Writes ``<build-dir>/index.html`` in place.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

GZIP_SHIM = """<script>
/*
 * GitHub Pages keeps the engine binaries gzip-compressed (index.pck.gz /
 * index.wasm.gz) so the published site stays under the 1 GB cap. The deploy
 * workflow writes those members and leaves tiny placeholders at the plain
 * names. Redirect the engine's normal requests to the .gz members and decode
 * them in the browser; when no .gz is deployed (local dev, serve_web.py) this
 * silently falls back to the uncompressed file.
 */
(function () {
\tif (typeof DecompressionStream === 'undefined') {
\t\tconsole.warn('[size] DecompressionStream unavailable; using uncompressed engine files.');
\t\treturn;
\t}
\tvar originalFetch = window.fetch.bind(window);
\twindow.fetch = function (resource, init) {
\t\tvar url = typeof resource === 'string' ? resource : (resource && resource.url) || '';
\t\tif (!/\\.(?:wasm|pck)(?:[?#]|$)/i.test(url)) return originalFetch(resource, init);
\t\tvar u = new URL(url, location.href);
\t\tif (/\\.gz$/i.test(u.pathname)) return originalFetch(resource, init);
\t\tu.pathname += '.gz';
\t\treturn originalFetch(u.toString(), init).then(function (gz) {
\t\t\tif (!gz.ok) return originalFetch(resource, init);
\t\t\tvar headers = new Headers(gz.headers);
\t\t\theaders.set('Content-Type', /\\.wasm\\.gz$/i.test(u.pathname) ? 'application/wasm' : 'application/octet-stream');
\t\t\theaders.delete('Content-Encoding');
\t\t\theaders.delete('Content-Length');
\t\t\tvar encoding = (gz.headers.get('content-encoding') || '').toLowerCase();
\t\t\tvar body = encoding === 'gzip' ? gz.body : gz.body.pipeThrough(new DecompressionStream('gzip'));
\t\t\treturn new Response(body, { status: gz.status, statusText: gz.statusText, headers: headers });
\t\t}).catch(function () {
\t\t\treturn originalFetch(resource, init);
\t\t});
\t};
})();
</script>
"""

# Injected between the "missing features" branch and the normal start branch.
COI_ANCHOR = "\t} else {\n\t\tsetStatusMode('progress');"

COI_BLOCK = """\t} else if (!window.crossOriginIsolated) {
\t\t// Godot's WASM build transfers SharedArrayBuffers to its worker pool, so
\t\t// the page must be crossOriginIsolated (COOP/COEP). GitHub Pages and
\t\t// other static hosts cannot send those headers; stream-cache-sw.js
\t\t// injects them, and we reload once the worker controls this document.
\t\t// Without this the download reaches 100% and the page then freezes with
\t\t// "SharedArrayBuffer transfer requires self.crossOriginIsolated".
\t\tconst coiKey = 'il_coi_reloads';
\t\tconst coiReloads = parseInt(sessionStorage.getItem(coiKey) || '0', 10);
\t\tif ('serviceWorker' in navigator && coiReloads < 2) {
\t\t\tsessionStorage.setItem(coiKey, String(coiReloads + 1));
\t\t\tsetStatusMode('notice');
\t\t\tsetStatusNotice('正在啟用跨源隔離（Service Worker）…\\n完成後會自動重新整理。');
\t\t\tnavigator.serviceWorker.register('stream-cache-sw.js').then(function (registration) {
\t\t\t\tif (navigator.serviceWorker.controller || registration.active) {
\t\t\t\t\twindow.location.reload();
\t\t\t\t\treturn;
\t\t\t\t}
\t\t\t\tnavigator.serviceWorker.addEventListener('controllerchange', function () {
\t\t\t\t\twindow.location.reload();
\t\t\t\t});
\t\t\t\twindow.setTimeout(function () {
\t\t\t\t\twindow.location.reload();
\t\t\t\t}, 4000);
\t\t\t}, function (err) {
\t\t\t\tdisplayFailureNotice('無法啟用跨源隔離：' + err.message);
\t\t\t});
\t\t} else {
\t\t\tdisplayFailureNotice('此瀏覽器無法提供 crossOriginIsolated（COOP/COEP），因此無法執行 WebAssembly 版本。\\n請改用 Chrome / Edge，並確認 Service Worker 未被停用。');
\t\t}
\t} else {
\t\tsessionStorage.removeItem('il_coi_reloads');
\t\tsetStatusMode('progress');"""

PROBE_ANCHOR = '\t\t<script src="index.js"></script>'

# Freeze triage. The game's own watchdog logs [Freeze] gaps from inside the
# Godot main loop; these probes cover the two cases that watchdog cannot see:
# a blocked browser main thread (no script runs at all) and a lost WebGL
# context (the canvas stops updating while the page stays responsive). Keep the
# marker text stable: bug reports are matched on it.
PROBE = """<script>
(function () {
\tvar MAX_EVENTS = 40;
\tvar events = 0;
\tvar t0 = 0;
\tfunction note(text) {
\t\tif (events++ >= MAX_EVENTS) return;
\t\tvar secs = t0 ? ((nowMs() - t0) / 1000).toFixed(1) : '?';
\t\ttry { console.error('[Freeze] ' + text + ' t=' + secs + 's'); } catch (e) {}
\t}
\tfunction nowMs() {
\t\treturn (window.performance && performance.now) ? performance.now() : Date.now();
\t}
\tt0 = nowMs();
\tvar last = t0;
\tsetInterval(function () {
\t\tvar now = nowMs();
\t\tvar gap = now - last;
\t\tlast = now;
\t\tif (gap < 1000 || gap > 60000) return;
\t\tif (document.visibilityState === 'hidden') return;
\t\tnote('js-gap=' + Math.round(gap) + 'ms');
\t}, 250);

\tvar canvas = document.getElementById('canvas');
\tif (!canvas) return;
\tcanvas.addEventListener('webglcontextlost', function () {
\t\tnote('webgl-context-lost');
\t\tshowContextLost();
\t}, false);
\tcanvas.addEventListener('webglcontextrestored', function () {
\t\tnote('webgl-context-restored');
\t}, false);

\tfunction showContextLost() {
\t\tif (document.getElementById('gl-lost')) return;
\t\tvar box = document.createElement('div');
\t\tbox.id = 'gl-lost';
\t\tbox.style.cssText = 'position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;background:rgba(8,10,14,.92);color:#e6edf3;font:14px/1.6 system-ui,sans-serif;text-align:center;padding:24px;';
\t\tbox.innerHTML = '<div><div style="font-size:18px;font-weight:600;margin-bottom:8px">畫面已停止更新</div>'
\t\t\t+ '<div style="color:#9fb0c3;margin-bottom:14px">顯示卡資源不足或驅動程式重設了 WebGL 內容，遊戲需要重新載入。</div>'
\t\t\t+ '<button id="gl-reload" style="font:inherit;padding:8px 18px;border-radius:6px;border:1px solid #3b4a5c;background:#1f6feb;color:#fff;cursor:pointer">重新載入</button></div>';
\t\tdocument.body.appendChild(box);
\t\tvar btn = document.getElementById('gl-reload');
\t\tif (btn) btn.onclick = function () { window.location.reload(); };
\t}
})();
</script>
"""

SIZES_RE = re.compile(r'"fileSizes":\{"index\.pck":\d+,"index\.wasm":\d+\}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--build-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "IdleLineage_Source_Restored" / "build" / "web_csharp",
        help="exported web build directory (default: the sibling source repo)",
    )
    ap.add_argument(
        "--pck",
        type=Path,
        help="pack whose byte size is reported to the loader (default: <build-dir>/index.pck)",
    )
    args = ap.parse_args()

    build_dir: Path = args.build_dir
    html_path = build_dir / "index.html"
    pck = args.pck or build_dir / "index.pck"
    wasm = build_dir / "index.wasm"
    for path in (html_path, pck, wasm):
        if not path.is_file():
            raise SystemExit(f"missing {path}")

    html = html_path.read_text(encoding="utf-8")

    # 1. Engine binaries are served as .gz; fetch shim goes right before </head>.
    if "DecompressionStream" not in html:
        head_end = "\t</head>"
        if html.count(head_end) != 1:
            raise SystemExit("could not locate </head>")
        html = html.replace(head_end, GZIP_SHIM + head_end, 1)

    # 2. crossOriginIsolated gate before starting the engine.
    if "crossOriginIsolated" not in html:
        if html.count(COI_ANCHOR) != 1:
            raise SystemExit("could not locate the engine start branch")
        html = html.replace(COI_ANCHOR, COI_BLOCK, 1)

    # 3. Freeze probes, before the engine loader script.
    if "[Freeze]" not in html:
        if html.count(PROBE_ANCHOR) != 1:
            raise SystemExit("could not locate the index.js script tag")
        html = html.replace(PROBE_ANCHOR, PROBE + PROBE_ANCHOR, 1)

    # 4. Report the sizes that are actually deployed (progress bar / cache).
    sizes = '"fileSizes":{"index.pck":%d,"index.wasm":%d}' % (pck.stat().st_size, wasm.stat().st_size)
    html, count = SIZES_RE.subn(sizes, html, count=1)
    if count != 1:
        raise SystemExit("could not locate fileSizes in GODOT_CONFIG")

    html_path.write_text(html, encoding="utf-8", newline="\n")
    print(f"patched {html_path}")
    print(f"  index.pck  {pck.stat().st_size:,} bytes")
    print(f"  index.wasm {wasm.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
