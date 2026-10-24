"""The room in a browser: pick who sits in it and their roles, watch it live, steer it.

Serves on 127.0.0.1 only, behind a random token in the link. Standard library only: a threaded HTTP server, one
page, JSON endpoints polled by the page. The room runs in a background thread; the owner's levers set flags.
"""
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import room as roomlib


class State:
    def __init__(self, agents, record_dir, judge, pace_s):
        self.agents, self.record_dir, self.judge, self.pace_s = agents, record_dir, judge, pace_s
        self.room, self.target, self.thread, self.ended = None, 0, None, False

    def start(self, body):
        topic = str(body.get("topic") or "").strip()
        fmt = body.get("format")
        seats = body.get("seats") or []
        if not topic or fmt not in roomlib.PHASES or not seats:
            raise ValueError("topic, format and at least one seat are needed")
        if any(s.get("name") not in self.agents for s in seats):
            raise ValueError("unknown agent")
        budget = body.get("budget_usd")
        self.room = roomlib.Room(topic, [roomlib.Seat(self.agents[s["name"]], str(s.get("role") or "ideas")[:60])
                                         for s in seats], fmt=fmt,
                                 budget_usd=float(budget) if budget else None, judge=self.judge,
                                 record_dir=self.record_dir)
        self.target, self.ended = len(roomlib.PHASES[fmt]), False
        self.room._emit("started", topic=topic, format=fmt,
                        seats=[{"name": s.agent.name, "role": s.role} for s in self.room.seats])
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        r, done = self.room, False
        while not r.closed:
            if not r.paused and (r.rounds < self.target or r.forced):
                done = False
                if not r.step() and r.over:
                    break
                time.sleep(self.pace_s)
            else:
                if r.rounds >= self.target and not done:
                    done = True
                    r._emit("waiting", text="All rounds done: one more round, or end for a summary.")
                time.sleep(0.05)

    def control(self, body):
        r, a = self.room, body.get("action")
        if r is None:
            raise ValueError("no room yet")
        if a == "pause":
            r.pause()
        elif a == "resume":
            r.resume()
        elif a == "inject":
            r.inject(str(body.get("text") or "")[:2000])
        elif a == "next":
            r.next_speaker(body.get("name"))
        elif a == "deeper":
            r.deeper(str(body.get("text") or "")[:500])
        elif a == "more":
            self.target = r.rounds + 1
        elif a == "end":
            if not self.ended:
                self.ended = True
                threading.Thread(target=r.end, daemon=True).start()
        else:
            raise ValueError("unknown action")


def handler_for(state, token):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _out(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json"
                                                         else body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _ok(self):
            q = parse_qs(urlparse(self.path).query)
            if not secrets.compare_digest(q.get("t", [""])[0], token):
                self._out(403, {"error": "forbidden"})
                return None
            return q

        def do_GET(self):
            q = self._ok()
            if q is None:
                return
            path = urlparse(self.path).path
            if path == "/":
                return self._out(200, PAGE, "text/html")
            if path == "/api/roster":
                return self._out(200, {"agents": list(state.agents), "roles": list(roomlib.ROLES),
                                       "formats": list(roomlib.PHASES), "judge": state.judge is not None})
            if path == "/api/events":
                ev = state.room.events if state.room else []
                since = int(q.get("since", ["0"])[0] or 0)
                return self._out(200, {"events": ev[since:], "next": len(ev),
                                       "spent": round(state.room.spent, 4) if state.room else 0})
            self._out(404, {"error": "not found"})

        def do_POST(self):
            if self._ok() is None:
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/start":
                    state.start(body)
                elif path == "/api/control":
                    state.control(body)
                else:
                    return self._out(404, {"error": "not found"})
            except (ValueError, KeyError, TypeError) as e:
                return self._out(400, {"error": str(e)[:200]})
            self._out(200, {"ok": True})
    return H


def serve(agents, record_dir=None, judge=None, port=8765, pace_s=0.3, background=True):
    token = secrets.token_urlsafe(16)
    state = State(agents, record_dir, judge, pace_s)
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler_for(state, token))
    srv.token, srv.state = token, state
    srv.url = f"http://127.0.0.1:{srv.server_port}/?t={token}"
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>herald room</title>
<style>
:root{--bg:#f6f5f2;--fg:#1d1d1f;--mute:#6b6b70;--card:#fff;--line:#e3e1dc;--acc:#3a5bd9}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececee;--mute:#9a9aa2;--card:#1e1e22;--line:#2c2c32;--acc:#8aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{display:grid;grid-template-columns:320px 1fr;gap:16px;padding:16px;max-width:1200px;margin:auto}
@media (max-width:800px){main{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
h1{font-size:17px;margin:0 0 10px}label{display:block;font-size:13px;color:var(--mute);margin:10px 0 4px}
input,select,textarea,button{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);
border-radius:7px;padding:6px 8px;width:100%}button{cursor:pointer;width:auto}button.main{background:var(--acc);color:#fff;border:0}
.seat{display:grid;grid-template-columns:auto 1fr 1fr;gap:6px;align-items:center;margin:4px 0}.seat input[type=checkbox]{width:auto}
#feed{display:flex;flex-direction:column;gap:10px;max-height:70vh;overflow:auto}
.msg{border-left:4px solid var(--c,var(--acc));padding:6px 10px;background:var(--bg);border-radius:6px;white-space:pre-wrap}
.who{font-weight:600}.role{color:var(--mute);font-size:12px;margin-left:6px}.sys{color:var(--mute);font-size:13px}
.bar{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}.bar input{flex:1;min-width:160px}
#summary{white-space:pre-wrap;border-top:1px solid var(--line);margin-top:10px;padding-top:10px}
</style></head><body><main>
<section><h1>Room</h1>
<label>Topic</label><textarea id="topic" rows="2" placeholder="What should the room think about?"></textarea>
<label>Format</label><select id="fmt"></select>
<label>Budget, $</label><input id="budget" type="number" step="0.05" value="0.50">
<label>Who sits in the room, and their role this time</label><div id="seats"></div>
<p class="sys" id="judge"></p><button class="main" onclick="start()">Start</button></section>
<section><h1 id="title">Not started</h1><div class="sys" id="status"></div>
<div class="bar"><button onclick="ctl('pause')">Pause</button><button onclick="ctl('resume')">Resume</button>
<button onclick="ctl('more')">One more round</button><button onclick="ctl('end')">End and sum up</button></div>
<div class="bar"><input id="say" placeholder="Say something into the room"><button onclick="say()">Say</button></div>
<div class="bar"><select id="next" style="width:auto"></select><button onclick="nxt()">Speaks next</button>
<input id="deep" placeholder="Go deeper on..."><button onclick="deeper()">Deeper</button></div>
<div id="feed"></div><div id="summary"></div></section></main>
<script>
const T=new URLSearchParams(location.search).get('t'),q=p=>p+(p.includes('?')?'&':'?')+'t='+T;
const api=(p,b)=>fetch(q(p),b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{}).then(r=>r.json());
const colors=['#3a5bd9','#c2410c','#15803d','#9333ea','#b45309','#0e7490','#be185d'];let idx={},since=0,roster;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const md=s=>esc(s).replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>').replace(/`([^`\n]+)`/g,'<code>$1</code>');  // after escaping: safe
api('/api/roster').then(r=>{roster=r;fmt.innerHTML=r.formats.map(f=>`<option>${f}</option>`).join('');
 seats.innerHTML=r.agents.map((a,i)=>{idx[a]=colors[i%colors.length];return `<div class="seat"><input type="checkbox" checked data-a="${esc(a)}">
 <span style="color:${idx[a]}">${esc(a)}</span><select>${r.roles.map(x=>`<option>${x}</option>`).join('')}</select></div>`}).join('');
 next.innerHTML=r.agents.map(a=>`<option>${esc(a)}</option>`).join('');judge.textContent=r.judge?'Moderator: Jev checks every reply for attacks.':'Moderator: off (no TYPESAFE_API_KEY).'});
function start(){const s=[...seats.querySelectorAll('.seat')].filter(d=>d.querySelector('input').checked)
 .map(d=>({name:d.querySelector('input').dataset.a,role:d.querySelector('select').value}));
 api('/api/start',{topic:topic.value,format:fmt.value,budget_usd:budget.value,seats:s}).then(r=>{if(r.error)alert(r.error)})}
const ctl=a=>api('/api/control',{action:a});const say=()=>{api('/api/control',{action:'inject',text:sayEl().value});sayEl().value=''};
const sayEl=()=>document.getElementById('say');const nxt=()=>api('/api/control',{action:'next',name:next.value});
const deeper=()=>api('/api/control',{action:'deeper',text:deep.value});
function show(e){const f=feed;if(e.type==='message'){const c=e.speaker==='Owner'?'#888':idx[e.speaker];
 f.insertAdjacentHTML('beforeend',`<div class="msg" style="--c:${c}"><span class="who">${esc(e.speaker)}</span><span class="role">${esc(e.role)}</span>\n${md(e.text)}</div>`);f.scrollTop=1e9}
 else if(e.type==='started'){title.textContent=e.topic;f.innerHTML='';summary.textContent=''}
 else if(e.type==='summary'){summary.innerHTML='<b>Summary</b>\n\n'+md(e.text)}
 else if(e.type==='thinking'){status.textContent=e.speaker+' is thinking...'}
 else{const t={round:'Round '+e.n+' done. Next: '+e.next,moderated:e.speaker+"'s reply was sent back: about the idea, not the person",
 budget:'Budget reached',paused:'Paused',resumed:'Resumed',waiting:e.text,focus:'Going deeper on: '+e.text}[e.type];
 if(t)f.insertAdjacentHTML('beforeend',`<div class="sys">${esc(t)}</div>`)}}
setInterval(()=>api('/api/events?since='+since).then(r=>{r.events.forEach(show);since=r.next;
 if(r.events.length&&!r.events.some(e=>e.type==='thinking'))status.textContent='spent $'+r.spent.toFixed(4)}),800);
</script></body></html>"""
