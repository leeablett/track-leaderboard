"""Mock leaderboards with different pagination styles: http://127.0.0.1:<port>/<mode>/leaderboard

Used by run_pagination_tests.py. Each style has 7 pages of 5 rows (\"many\" has 250 pages)."""
import http.server, json, urllib.parse, time
PER = 5
MODES = {"app", "remember", "divpager", "blazor", "blazor_busy", "blazor_big", "formpost", "decoy", "iconnext", "veryslow", "lostclick", "next_text", "numbers", "mui", "slow", "slow_keep", "loadmore", "many", "bootstrap", "ssr_next_text"}
def pages_for(mode): return 250 if mode == "many" else 3 if mode in ("veryslow", "lostclick") else 7
ROW = "r => `<tr><td>${r.rank}</td><td>${r.name}</td><td>${r.score}</td></tr>`"
TABLE = "rows => '<table><thead><tr><th>Rank</th><th>Player</th><th>Score</th></tr></thead><tbody>' + rows.map(" + ROW + ").join('') + '</tbody></table>'"
ARROW_SVG = '<svg width="12" height="12" viewBox="0 0 12 12"><path d="M4 2l4 4-4 4" stroke="currentColor" fill="none"/></svg>'

def shell(mode):
    js_controls = {
        # "Next »" / "Next ›" text buttons
        "next_text": "pager.innerHTML = `<button id=prev ${p<=1?'disabled':''}>« Prev</button> Page ${p} of ${N} <button id=nxt ${p>=N?'disabled':''}>Next »</button>`; if (p<N) nxt.onclick = () => go(p+1);",
        # icon-only arrow button with no label, plus numbered buttons (windowed with …)
        "numbers": "let h=''; const win=[1,...[p-1,p,p+1].filter(x=>x>1&&x<N),N]; let last=0; for (const i of [...new Set(win)]) { if (i-last>1) h+='<span>…</span>'; h+=`<button class='pg ${i===p?'active':''}' data-p=${i}>${i}</button>`; last=i; } h+=`<button class=arrow ${p>=N?'disabled':''}>" + ARROW_SVG + "</button>`; pager.innerHTML=h; pager.querySelectorAll('.pg').forEach(b=>b.onclick=()=>go(+b.dataset.p)); if (p<N) pager.querySelector('.arrow').onclick=()=>go(p+1);",
        # Material-UI style: aria-label="Go to next page", icon only
        "mui": "pager.innerHTML = `<span>${(p-1)*5+1}–${Math.min(p*5,N*5)} of ${N*5}</span><button aria-label='Go to previous page' ${p<=1?'disabled':''}>" + ARROW_SVG + "</button><button aria-label='Go to next page' ${p>=N?'disabled':''} id=nx>" + ARROW_SVG + "</button>`; if (p<N) nx.onclick=()=>go(p+1);",
        # plain Next button, but the table is replaced by a spinner for 3 s while loading
        "slow": "pager.innerHTML = `<button id=nxt ${p>=N?'disabled':''}>Next</button>`; if (p<N) nxt.onclick = () => go(p+1, 3000, true);",
        # plain Next button, old table stays visible for 3 s while loading
        "slow_keep": "pager.innerHTML = `<button id=nxt ${p>=N?'disabled':''}>Next</button>`; if (p<N) nxt.onclick = () => go(p+1, 3000, false);",
        # "Load more" appends rows to the same table
        "loadmore": "pager.innerHTML = p<N ? `<button id=more>Load more</button>` : ''; if (p<N) more.onclick = () => go(p+1, 0, false, true);",
        # "<<  Page N  >>" made of plain <div>s (clicks wired up by script, no button/link) in a
        # small layout table above the leaderboard; elsewhere a "1 2 3" group of btn-primary
        # buttons (not page numbers) that reloads page 1
        "divpager": "roundbar.innerHTML = `<div class=btn-group>${[1,2,3].map(i => `<button class='btn btn-primary'>${i}</button>`).join('')}</div>`; roundbar.querySelectorAll('button').forEach(b => b.onclick = () => go(1)); pager.innerHTML = `<table class=nav><tr><td><div class=navbtn id=pv>&lt;&lt;</div></td><td>Page ${p}</td><td><div class=navbtn id=nx style='cursor:pointer'>&gt;&gt;</div></td></tr></table>`; if (p>1) pv.addEventListener('click', () => go(p-1)); if (p<N) nx.addEventListener('click', () => go(p+1));",
        # ">>" in the pagination bar is next; from page 2 a "›" arrow ABOVE the table (e.g. a
        # round/season switcher) also appears, and clicking it reloads page 1
        "decoy": "roundbar.innerHTML = p>=2 ? `<button id=rnd>›</button> Round 5` : 'Round 5'; if (p>=2) rnd.onclick = () => go(1); let h=''; for (let i=1;i<=Math.min(3,N);i++) h+=`<a href='#' class='pg' data-p=${i}>${i}</a> `; h+=`<span>…</span> <a href='#' id=nx ${p>=N?'disabled':''}>&gt;&gt;</a>`; pager.innerHTML=h; pager.querySelectorAll('.pg').forEach(b=>b.onclick=e=>{e.preventDefault();go(+b.dataset.p)}); if (p<N) nx.onclick=e=>{e.preventDefault();go(p+1)};",
        # ">>" drawn as an icon font glyph with no text, next to numbered pages 1-3 only
        "iconnext": "let h=''; for (let i=1;i<=Math.min(3,N);i++) h+=`<button class=pg data-p=${i}>${i}</button>`; h+=`<button class='btn' ${p>=N?'disabled':''} id=nx><i class='fa fa-angle-double-right'></i></button>`; pager.innerHTML=h; pager.querySelectorAll('.pg').forEach(b=>b.onclick=()=>go(+b.dataset.p)); if (p<N) nx.onclick=()=>go(p+1);",
        # each page takes 25 s to load (longer than the scraper's first wait)
        "veryslow": "pager.innerHTML = `<button id=nxt ${p>=N?'disabled':''}>Next</button>`; if (p<N) nxt.onclick = () => go(p+1, 25000, false);",
        # the site ignores the first click on each Next button
        "lostclick": "pager.innerHTML = `<button id=nxt ${p>=N?'disabled':''}>Next</button>`; let clicks = 0; if (p<N) nxt.onclick = () => { if (++clicks === 1) return; go(p+1); };",
        # many pages with a plain Next
        "many": "pager.innerHTML = `<button id=nxt ${p>=N?'disabled':''}>Next</button>`; if (p<N) nxt.onclick = () => go(p+1);",
        # Next is an <a> with class 'page-link' and text 'Next ›' (bootstrap), no href change (#)
        "bootstrap": "pager.innerHTML = `<ul class=pagination><li class='page-item ${p>=N?'disabled':''}'><a class=page-link href='#'>Next ›</a></li></ul>`; if (p<N) pager.querySelector('a').onclick = e => { e.preventDefault(); go(p+1); };",
    }[mode]
    return f"""<html><body><div id=roundbar></div><div id=app>Loading…</div><div id=pager></div><script>
let p = 1, N = {pages_for(mode)}, all = [];
const table = {TABLE};
async function go(n, delay=0, blank=false, append=false) {{
  if (blank) app.innerHTML = '<div class=spinner>Loading…</div>';
  if (delay) await new Promise(r => setTimeout(r, delay));
  const d = await (await fetch('/api?page=' + n)).json();
  p = n; all = append ? all.concat(d.rows) : d.rows;
  app.innerHTML = table(all);
  {js_controls}
}}
go(1);
</script></body></html>"""

BLAZOR = """<html><head><meta charset=utf-8></head><body style="background:#021;color:#9c6">
<div class=btn-group><button class="btn btn-primary">1</button><button class="btn btn-primary">2</button><button class="btn btn-primary">3</button></div>
<table class=board><tbody id=board></tbody></table>
<script>
// Copied from the real site's markup: the pager is the first row of the leaderboard table,
// "<<" / ">>" are <button class="player-name">, each player cell holds its own small table
// ("#1" | "NRG-DFC"), the headings are plain
// <td>s, and rows are swapped in place (Blazor). On the last page ">>" stays enabled and does nothing.
let p = 1; const N = 7;
async function go(n) {
  if (n < 1 || n > N) return;
  const d = await (await fetch('/api?page=' + n)).json(); p = n;
  board.innerHTML =
    `<tr b-gszbhyv0cx=""><td class="page-selector" b-gszbhyv0cx=""><button class="player-name" style="width:50px" b-gszbhyv0cx="">&lt;&lt;</button></td><!--!-->
     <td class="page-selector" b-gszbhyv0cx="">Page ${p}</td><!--!-->
     <td class="page-selector" b-gszbhyv0cx=""><button class="player-name" style="width:50px" b-gszbhyv0cx="">&gt;&gt;</button></td></tr>` +
    `<tr><td>Player</td><td>Rating</td><td>Win</td><td>Loss</td><td>Draw</td></tr>` +
    d.rows.map(r => `<tr><td><table><tr b-gszbhyv0cx=""><td class="player-name" style="width:20px" b-gszbhyv0cx="">#${r.rank}</td><!--!-->
       <td class="player-name" style="padding-left:10px;border-left-width:1px;" b-gszbhyv0cx="">${r.name.replace(' ', '')}</td></tr></table></td>
       <td>${r.score}</td><td>${r.rank % 9}</td><td>${r.rank % 4}</td><td>0</td></tr>`).join('');
  const b = board.querySelectorAll('.page-selector button');
  b[0].onclick = () => go(p - 1); b[1].onclick = () => go(p + 1);
}
// the "1 2 3" group elsewhere on the page is not pagination: it reloads page 1
document.querySelectorAll('.btn-group button').forEach(x => x.onclick = () => go(1));
go(1);
</script></body></html>"""


# Same page, but the connection to the server falls back to constant small requests (as Blazor
# does when its live connection is blocked), so the network never goes idle.
BLAZOR_BUSY = BLAZOR.replace("go(1);\n</script>", "go(1);\nsetInterval(() => fetch('/poll'), 200);\n</script>")
assert BLAZOR_BUSY != BLAZOR
# Full size, like the real leaderboard: 100 players a page, 12 pages.
BLAZOR_BIG = BLAZOR.replace("const N = 7;", "const N = 12;").replace("fetch('/api?page=' + n)", "fetch('/api?per=100&page=' + n)")
assert BLAZOR_BIG.count("per=100") == 1 and "const N = 12;" in BLAZOR_BIG


# Like the real site, but the page first loads a big, cacheable code file (as a Blazor
# WebAssembly app does), which a saved browser profile shouldn't download again.
FRAMEWORK = ("window.FRAMEWORK_LOADED = true;\n/*" + "x" * 2_000_000 + "*/\n").encode()
FRAMEWORK_DOWNLOADS = []  # one entry per download, so tests can count them
BLAZOR_APP = BLAZOR.replace("<script>\n// Copied", '<script src="/framework.js"></script>\n<script>\n// Copied')
assert BLAZOR_APP != BLAZOR
# Like the real site, but it remembers the page you were on (in the browser's local storage)
# and reopens there. A scraper reusing a profile must still start from page 1.
BLAZOR_REMEMBER = (BLAZOR.replace("let p = 1; const N = 7;", "let p = 1; const N = 7;")
                   .replace("p = n;\n", "p = n; localStorage.setItem('lbPage', n);\n")
                   .replace("go(1);\n</script>", "go(+localStorage.getItem('lbPage') || 1);\n</script>"))
assert BLAZOR_REMEMBER.count("lbPage") == 2


def form_page(p, n=7):
    """Like the real site: "<<  Page N  >>" form buttons above the table; each click POSTs the
    form and reloads the page. Addresses are only partly honoured: ?page=2 works, ?page=3+ shows page 1."""
    rows = "".join(f"<tr><td><div class=box>Player{r:03d}</div></td><td>{1600-r}</td><td>{r % 9}</td><td>{r % 4}</td><td>0</td></tr>"
                   for r in range((p-1)*PER+1, p*PER+1))
    nxt_dis = " disabled" if p >= n else ""
    prv_dis = " disabled" if p <= 1 else ""
    return f"""<html><head><meta charset=utf-8></head><body style="background:#021">
<form method=post><input type=hidden name=page value={p}>
<input type=submit name=nav value="&lt;&lt;"{prv_dis}> Page {p} <input type=submit name=nav value="&gt;&gt;"{nxt_dis}></form>
<table><tr><th>Player</th><th>Rating</th><th>Win</th><th>Loss</th><th>Draw</th></tr>{rows}</table></body></html>"""


def server_rendered(p):
    rows = "" if p > 7 else "".join(f"<tr><td>{r}</td><td>Player {r}</td><td>{1000-r}</td></tr>" for r in range((p-1)*PER+1, p*PER+1))
    nxt = f'<a class="page-link" href="?page={p+1}">Next ›</a>' if p < 7 else '<span class="page-link disabled">Next ›</span>'
    return f"<html><table><thead><tr><th>Rank</th><th>Player</th><th>Score</th></tr></thead><tbody>{rows}</tbody></table><nav>{nxt}</nav></html>"

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == "/api":
            p, per = int(q["page"][0]), int(q.get("per", [PER])[0])
            rows = [{"rank": r, "name": f"Player {r}", "score": 10000 - r} for r in range((p-1)*per+1, p*per+1)]
            body, ct = json.dumps({"rows": rows}).encode(), "application/json"
        else:
            mode = u.path.strip("/").split("/")[0]
            if mode == "poll":
                self.send_response(200); self.end_headers(); return
            if u.path == "/framework.js":
                FRAMEWORK_DOWNLOADS.append(1)
                time.sleep(1.0)  # a slow connection
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(FRAMEWORK)))
                self.end_headers(); self.wfile.write(FRAMEWORK); return
            if mode not in MODES:
                self.send_response(404); self.end_headers(); return
            if mode == "blazor":
                body = BLAZOR.encode()
            elif mode == "app":
                body = BLAZOR_APP.encode()
            elif mode == "remember":
                body = BLAZOR_REMEMBER.encode()
            elif mode == "blazor_big":
                body = BLAZOR_BIG.encode()
            elif mode == "blazor_busy":
                body = BLAZOR_BUSY.encode()
            elif mode == "formpost":
                asked = int(q.get("page", ["1"])[0])
                body = form_page(asked if asked <= 2 else 1).encode()
            else:
                body = (server_rendered(int(q.get("page", ["1"])[0])) if mode == "ssr_next_text" else shell(mode)).encode()
            ct = "text/html; charset=utf-8"
        self.send_response(200); self.send_header("Content-Type", ct); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        p = int(form.get("page", ["1"])[0])
        p = min(7, p + 1) if form.get("nav", [""])[0] == ">>" else max(1, p - 1)
        body = form_page(p).encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a): pass


def start(port: int = 0):
    """Start the mock sites in a background thread; returns the server (server.server_port)."""
    import threading
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", 8770), H).serve_forever()
