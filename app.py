"""
Life RPG — Living Narrative OS v4
Parchment · Knowledge Graph · Колесо Миров · AI Архивариус
"""
import json, uuid, re, subprocess, time, threading, os
import urllib.request as _ur
from datetime import datetime
from pathlib import Path

import kuzu
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

APP_CFG_FILE = Path(__file__).parent / "app_config.json"

def _app_cfg():
    if APP_CFG_FILE.exists():
        try: return json.loads(APP_CFG_FILE.read_text())
        except: pass
    return {}

def _get_api_key():
    return _app_cfg().get("api_key") or os.environ.get("ANTHROPIC_API_KEY","")

_gc_token_cache = {"token": "", "expires": 0.0}

def _get_gigachat_token(key: str, scope: str) -> str:
    global _gc_token_cache
    if _gc_token_cache["token"] and time.time() < _gc_token_cache["expires"] - 60:
        return _gc_token_cache["token"]
    try:
        import httpx, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        r = httpx.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={"Authorization": f"Basic {key}",
                     "RqUID": str(uuid.uuid4()),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"scope": scope},
            verify=False, timeout=15)
        d = r.json()
        tok = d.get("access_token","")
        exp = d.get("expires_at", 0) / 1000.0  # ms → s
        _gc_token_cache = {"token": tok, "expires": exp or time.time()+1700}
        return tok
    except Exception as e:
        print(f"[gigachat_token] {e}"); return ""

def _call_gigachat(prompt_text: str) -> str:
    cfg = _app_cfg()
    key = cfg.get("gigachat_key",""); scope = cfg.get("gigachat_scope","GIGACHAT_API_PERS")
    if not key: return ""
    tok = _get_gigachat_token(key, scope)
    if not tok: return ""
    try:
        import httpx
        r = httpx.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            json={"model":"GigaChat","messages":[{"role":"user","content":prompt_text}],
                  "max_tokens":2048,"temperature":0.3},
            verify=False, timeout=60)
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[gigachat] {e}"); return ""

def call_ai_extract(raw: str) -> dict:
    """Call AI (Anthropic or GigaChat). Returns parsed dict or None."""
    ent_ctx, miss_ctx = build_context()
    p = PROMPT.format(raw=raw, entities_ctx=ent_ctx or "нет",
                      missions_ctx=miss_ctx or "нет активных путей")
    # Try Anthropic first
    ant_key = _get_api_key()
    if ant_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ant_key)
            msg = client.messages.create(model="claude-haiku-4-5", max_tokens=2048,
                messages=[{"role":"user","content":p}])
            text = msg.content[0].text if msg.content else ""
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m: return json.loads(m.group())
        except Exception as e: print(f"[anthropic] {e}")
    # Try GigaChat
    cfg = _app_cfg()
    if cfg.get("gigachat_key"):
        try:
            text = _call_gigachat(p)
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m: return json.loads(m.group())
        except Exception as e: print(f"[gigachat_parse] {e}")
    return None

def call_claude_extract(raw: str) -> dict:  # backward compat alias
    return call_ai_extract(raw)

DB_PATH   = str(Path(__file__).parent / "liferpg.db")
_VER_F    = Path(__file__).parent / ".schema_v"
_SCHEMA   = "5"   # bump → auto-drops all tables and rebuilds

_db   = kuzu.Database(DB_PATH)
_conn = kuzu.Connection(_db)

def _drop_all():
    for t in ["LINKED","MENTIONS"]:      # rels first
        try: _conn.execute(f"DROP TABLE {t}")
        except: pass
    for t in ["Entry","Entity","Mission","Task","Finance","Mode"]:
        try: _conn.execute(f"DROP TABLE {t}")
        except: pass

def _setup():
    cv = _VER_F.read_text().strip() if _VER_F.exists() else "0"
    if cv != _SCHEMA:
        _drop_all()
        _VER_F.write_text(_SCHEMA)
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Entry(
        id STRING, ts STRING, raw_text STRING, narrative STRING,
        archivist_note STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Entity(
        id STRING, name STRING, type STRING, summary STRING, tags STRING,
        PRIMARY KEY(id))""")
    _conn.execute("CREATE REL TABLE IF NOT EXISTS MENTIONS(FROM Entry TO Entity)")
    _conn.execute("""CREATE REL TABLE IF NOT EXISTS LINKED(
        FROM Entity TO Entity, label STRING, entry_id STRING)""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Mission(
        id STRING, title STRING, description STRING, status STRING,
        ts STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Task(
        id STRING, mission_id STRING, title STRING, status STRING,
        ts STRING, entry_id STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Finance(
        id STRING, amount DOUBLE, direction STRING, category STRING,
        note STRING, ts STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Mode(
        id STRING, name STRING, description STRING, active STRING,
        started_ts STRING, PRIMARY KEY(id))""")
_setup()

def _migrate_task_columns():
    """Add new columns without dropping data (idempotent)."""
    for col, dtype, default in [
        ("task_type",      "STRING", "'once'"),
        ("reset_hours",    "INT64",  "24"),
        ("required_iters", "INT64",  "1"),
        ("current_iters",  "INT64",  "0"),
        ("last_reset_ts",  "STRING", "''"),
        ("streak",         "INT64",  "0"),
        ("best_streak",    "INT64",  "0"),
    ]:
        try: _conn.execute(f"ALTER TABLE Task ADD {col} {dtype} DEFAULT {default}")
        except: pass
    try: _conn.execute("ALTER TABLE Mission ADD lore STRING DEFAULT ''")
    except: pass
    try: _conn.execute("ALTER TABLE Task ADD completed_ts STRING DEFAULT ''")
    except: pass
_migrate_task_columns()

def _maybe_reset_task(t: dict) -> dict:
    """Check if repeatable task cycle is over; update DB and return updated dict."""
    if t.get("task_type") != "repeat" or not t.get("last_reset_ts"):
        return t
    try:
        from datetime import timedelta
        last = datetime.strptime(t["last_reset_ts"], "%Y-%m-%d %H:%M")
        due  = last + timedelta(hours=int(t.get("reset_hours", 24)))
        if datetime.now() < due:
            return t
        # Reset cycle
        cur  = int(t.get("current_iters", 0))
        req  = int(t.get("required_iters", 1))
        stk  = int(t.get("streak", 0))
        best = int(t.get("best_streak", 0))
        if cur >= req: stk += 1; best = max(best, stk)
        else:          stk = 0
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
        _conn.execute(
            "MATCH (t:Task) WHERE t.id=$id "
            "SET t.current_iters=0,t.last_reset_ts=$ts,t.streak=$s,t.best_streak=$b",
            {"id": t["id"], "ts": now_s, "s": stk, "b": best})
        t.update({"current_iters": 0, "last_reset_ts": now_s, "streak": stk, "best_streak": best})
    except: pass
    return t

# ── Helpers ───────────────────────────────────────────────────────────────────
def slug(name):
    s = re.sub(r"[\s\-]+","_", name.lower().strip())
    return re.sub(r"[^a-z0-9а-яё_]","",s) or "unknown"

def kuzu_rows(r):
    rows=[]
    while r.has_next(): rows.append(r.get_next())
    return rows

def entity_exists(eid):
    r=_conn.execute("MATCH (e:Entity) WHERE e.id=$id RETURN count(e)",{"id":eid})
    return r.get_next()[0]>0 if r.has_next() else False

def build_context():
    ents = kuzu_rows(_conn.execute(
        "MATCH (e:Entity) RETURN e.name,e.type,e.summary LIMIT 40"))
    missions = kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.status='active' RETURN m.id,m.title LIMIT 10"))
    ent_ctx  = "\n".join(f"- {r[0]} ({r[1]}): {r[2]}" for r in ents)
    miss_ctx = "\n".join(f"  ID={r[0]}: {r[1]}" for r in missions)
    return ent_ctx, miss_ctx

PROMPT = """Ты — Архивариус, хранитель Живой Летописи. Анализируй запись Героя и верни ТОЛЬКО валидный JSON без markdown.

АКТИВНЫЕ МЕЧТЫ ГЕРОЯ:
{missions_ctx}

ИЗВЕСТНЫЕ СУЩНОСТИ В ЛЕТОПИСИ:
{entities_ctx}

Верни JSON строго в таком формате:
{{
  "narrative": "2-4 предложения от третьего лица — стиль Morrowind, хроника, величие, конкретные детали",
  "entities": [
    {{"name":"Имя","type":"person|place|concept|project|event","summary":"одно ёмкое предложение","tags":[]}}
  ],
  "relations": [
    {{"from_entity":"Имя1","to_entity":"Имя2","label":"глагол отношения"}}
  ],
  "quests": [
    {{"title":"конкретный шаг или задача","mission_id":"id пути если явно связано или пустая строка","description":"контекст","task_type":"once или repeat","reset_hours":24,"required_iters":1}}
  ],
  "archivist_note": "1-2 предложения — мудрость Архивариуса о значении этой записи для судьбы Героя. Нарративно, эпически.",
  "mission_analysis": [
    {{"mission_id":"id","insight":"что эта запись даёт или блокирует","lore":"1-2 предложения в стиле летописи Морровинда — что этот Путь значит для Героя, конкретно и нарративно"}}
  ]
}}

ПРАВИЛА:
- entities: только реально упомянутые люди, места, идеи, проекты — не выдумывай
- relations: только если связь явно следует из текста
- quests: только конкретные задачи/обязательства, явно упоминаемые
- Если сущность уже есть в летописи — обновляй summary, не дублируй
- mission_id в quests: точный UUID из списка путей выше, или "" если неясно
- task_type: "repeat" если задача ежедневная/регулярная/повторяемая, иначе "once"
- reset_hours: интервал в часах между сбросами (24 = ежедневно, 168 = еженедельно и т.д.)
- required_iters: сколько раз нужно выполнить задачу за один цикл (обычно 1, но может быть больше)
- Никогда не добавляй поля вне схемы

ЗАПИСЬ ГЕРОЯ:
{raw}"""

def extract(raw):
    ent_ctx, miss_ctx = build_context()
    p = PROMPT.format(raw=raw,
                      entities_ctx=ent_ctx or "нет",
                      missions_ctx=miss_ctx or "нет активных путей")
    try:
        r = subprocess.run(["claude","-p",p], capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            m = re.search(r'\{.*\}', r.stdout.strip(), re.DOTALL)
            if m: return json.loads(m.group())
    except Exception:
        pass
    return {"narrative":raw,"entities":[],"relations":[],"quests":[],
            "archivist_note":"","mission_analysis":[],"_ai_pending":True}

def write_entry(raw, data):
    eid = str(uuid.uuid4())
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M")
    an  = data.get("archivist_note","")
    _conn.execute(
        "CREATE (:Entry {id:$id,ts:$ts,raw_text:$r,narrative:$n,archivist_note:$an})",
        {"id":eid,"ts":ts,"r":raw,"n":data.get("narrative",raw),"an":an})
    for ent in data.get("entities",[]):
        sid  = slug(ent["name"])
        tags = json.dumps(ent.get("tags",[]), ensure_ascii=False)
        if entity_exists(sid):
            _conn.execute(
                "MATCH (e:Entity) WHERE e.id=$id SET e.summary=$s, e.tags=$t",
                {"id":sid,"s":ent["summary"],"t":tags})
        else:
            _conn.execute(
                "CREATE (:Entity {id:$id,name:$name,type:$type,summary:$summary,tags:$tags})",
                {"id":sid,"name":ent["name"],"type":ent.get("type","concept"),
                 "summary":ent["summary"],"tags":tags})
        try:
            _conn.execute(
                "MATCH (en:Entry) WHERE en.id=$eid MATCH (et:Entity) WHERE et.id=$etid"
                " CREATE (en)-[:MENTIONS]->(et)",
                {"eid":eid,"etid":sid})
        except: pass
    for rel in data.get("relations",[]):
        f = slug(rel.get("from_entity",""))
        t = slug(rel.get("to_entity",""))
        if f and t and entity_exists(f) and entity_exists(t):
            try:
                _conn.execute(
                    "MATCH (a:Entity) WHERE a.id=$f MATCH (b:Entity) WHERE b.id=$t"
                    " CREATE (a)-[:LINKED{label:$l,entry_id:$eid}]->(b)",
                    {"f":f,"t":t,"l":rel.get("label","связан с"),"eid":eid})
            except: pass
    return eid

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI()

_moon_cache: dict = {"data":None,"ts":0.0}

@app.get("/moonphase")
def moonphase():
    global _moon_cache
    now = time.time()
    if _moon_cache["data"] and now-_moon_cache["ts"]<21600:
        return _moon_cache["data"]
    import ssl
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi; ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
        req = _ur.Request("https://wttr.in/?format=j1",
            headers={"User-Agent":"LifeRPG/1.0","Accept":"application/json"})
        with _ur.urlopen(req,timeout=8,context=ctx) as resp:
            j=json.loads(resp.read()); a=j["weather"][0]["astronomy"][0]
            result={"phase_en":a["moon_phase"],"illumination":int(a["moon_illumination"]),
                    "moonrise":a.get("moonrise"),"moonset":a.get("moonset"),
                    "sunrise":a.get("sunrise"),"sunset":a.get("sunset"),
                    "source":"wttr.in","cached_at":datetime.now().strftime("%Y-%m-%d %H:%M")}
        _moon_cache={"data":result,"ts":now}; return result
    except Exception as ex:
        return {"phase_en":None,"illumination":None,"source":"math_fallback","error":str(ex)}

class IngestReq(BaseModel): text: str
class SaveReq(BaseModel):
    raw_text: str; narrative: str; entities: list; relations: list
    quests: list = []; archivist_note: str = ""; mission_analysis: list = []
class MissionReq(BaseModel): title: str; description: str = ""
class TaskReq(BaseModel):
    mission_id: str; title: str
    task_type: str = "once"; reset_hours: int = 24; required_iters: int = 1
class FinanceReq(BaseModel): amount: float; direction: str; category: str=""; note: str=""
class ModeReq(BaseModel): name: str; description: str = ""

INBOX_FILE = Path(__file__).parent / "inbox.json"

@app.post("/ingest")
def ingest(req: IngestReq):
    eid = write_entry(req.text, {"narrative": req.text, "entities": [], "relations": [],
                                  "archivist_note": "", "quests": []})
    if _has_any_ai():
        # AI available — process in background
        threading.Thread(target=_process_entry_bg, args=(eid, req.text), daemon=True).start()
        # Still write to inbox so JS polling works (removed on completion)
        try:
            inbox = json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
            inbox.append({"id": eid, "text": req.text, "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
            INBOX_FILE.write_text(json.dumps(inbox, ensure_ascii=False, indent=2))
        except: pass
        return {"entry_id": eid, "status": "processing", "_ai_pending": True}
    else:
        # No API key — write to inbox for manual processing
        try:
            inbox = json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
            inbox.append({"id": eid, "text": req.text, "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
            INBOX_FILE.write_text(json.dumps(inbox, ensure_ascii=False, indent=2))
        except: pass
        return {"entry_id": eid, "status": "pending", "_ai_pending": True}

@app.get("/inbox")
def get_inbox():
    if not INBOX_FILE.exists(): return []
    try: return json.loads(INBOX_FILE.read_text())
    except: return []

@app.post("/inbox/clear")
def clear_inbox():
    if INBOX_FILE.exists(): INBOX_FILE.write_text("[]")
    return {"ok": True}

@app.post("/inbox/process-pending")
def process_pending():
    """Trigger background processing for all stuck inbox entries (no new entries created)."""
    if not _has_any_ai():
        return {"ok": False, "reason": "no_ai"}
    try:
        inbox = json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
        pending = [i for i in inbox if not i.get("type") or i.get("type") == "entry"]
        for item in pending:
            eid = item.get("id","")
            text = item.get("text","") or item.get("raw","")
            if eid and text:
                threading.Thread(target=_process_entry_bg, args=(eid, text), daemon=True).start()
        return {"ok": True, "started": len(pending)}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

def _apply_analysis(eid: str, data: dict):
    """Apply AI analysis result to DB (shared by update_entry and auto-processing)."""
    narrative = data.get("narrative","")
    an = data.get("archivist_note","")
    if narrative:
        try:
            _conn.execute(
                "MATCH (e:Entry) WHERE e.id=$id SET e.narrative=$n, e.archivist_note=$an",
                {"id": eid, "n": narrative, "an": an})
        except: pass
    for ent in data.get("entities",[]):
        sid = slug(ent["name"])
        tags = json.dumps(ent.get("tags",[]), ensure_ascii=False)
        if entity_exists(sid):
            _conn.execute("MATCH (e:Entity) WHERE e.id=$id SET e.summary=$s, e.tags=$t",
                          {"id":sid,"s":ent["summary"],"t":tags})
        else:
            _conn.execute("CREATE (:Entity {id:$id,name:$name,type:$type,summary:$summary,tags:$tags})",
                          {"id":sid,"name":ent["name"],"type":ent.get("type","concept"),
                           "summary":ent["summary"],"tags":tags})
        try:
            _conn.execute(
                "MATCH (en:Entry) WHERE en.id=$eid MATCH (et:Entity) WHERE et.id=$etid"
                " CREATE (en)-[:MENTIONS]->(et)", {"eid":eid,"etid":sid})
        except: pass
    for rel in data.get("relations",[]):
        f=slug(rel.get("from_entity","")); t=slug(rel.get("to_entity",""))
        if f and t and entity_exists(f) and entity_exists(t):
            try:
                _conn.execute(
                    "MATCH (a:Entity) WHERE a.id=$f MATCH (b:Entity) WHERE b.id=$t"
                    " CREATE (a)-[:LINKED{label:$l,entry_id:$eid}]->(b)",
                    {"f":f,"t":t,"l":rel.get("label","связан с"),"eid":eid})
            except: pass
    for q in data.get("quests",[]):
        tid=str(uuid.uuid4()); ts=datetime.now().strftime("%Y-%m-%d %H:%M")
        tt=q.get("task_type","once"); rh=int(q.get("reset_hours",24)); ri=int(q.get("required_iters",1))
        lr=ts if tt=="repeat" else ""
        try:
            _conn.execute(
                "CREATE (:Task {id:$id,mission_id:$mid,title:$t,status:'active',ts:$ts,entry_id:$eid,"
                "task_type:$tt,reset_hours:$rh,required_iters:$ri,current_iters:0,last_reset_ts:$lr,streak:0,best_streak:0,completed_ts:''})",
                {"id":tid,"mid":q.get("mission_id",""),"t":q["title"],"ts":ts,"eid":eid,
                 "tt":tt,"rh":rh,"ri":ri,"lr":lr})
        except: pass
    for ma in data.get("mission_analysis",[]):
        lore = ma.get("lore","").strip()
        if lore and ma.get("mission_id"):
            try:
                _conn.execute("MATCH (m:Mission) WHERE m.id=$id SET m.lore=$l",
                              {"id":ma["mission_id"],"l":lore})
            except: pass

def _process_entry_bg(eid: str, raw: str):
    """Background thread: call Claude, apply analysis, remove from inbox."""
    try:
        data = call_claude_extract(raw)
        if data:
            _apply_analysis(eid, data)
    except Exception as e:
        print(f"[process_bg] {e}")
    finally:
        # Remove from inbox regardless of success
        try:
            inbox = json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
            inbox = [i for i in inbox if i.get("id") != eid]
            INBOX_FILE.write_text(json.dumps(inbox, ensure_ascii=False, indent=2))
        except: pass

@app.post("/diary/{eid}/update")
def update_entry(eid: str, req: SaveReq):
    """Архивариус (manual/external) обновляет запись после анализа"""
    data = {"narrative": req.narrative, "archivist_note": req.archivist_note,
            "entities": req.entities, "relations": req.relations,
            "quests": req.quests, "mission_analysis": req.mission_analysis}
    _apply_analysis(eid, data)
    return {"updated": eid, "quests_created": len(req.quests)}

@app.post("/save")
def save(req: SaveReq):
    data={"narrative":req.narrative,"entities":req.entities,"relations":req.relations,
          "quests":req.quests,"archivist_note":req.archivist_note}
    eid=write_entry(req.raw_text,data)
    for q in req.quests:
        tid=str(uuid.uuid4()); ts=datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            _conn.execute(
                "CREATE (:Task {id:$id,mission_id:$mid,title:$t,status:'active',ts:$ts,entry_id:$eid})",
                {"id":tid,"mid":q.get("mission_id",""),"t":q["title"],"ts":ts,"eid":eid})
        except: pass
    return {"entry_id":eid,"quests_created":len(req.quests)}

@app.get("/diary")
def diary(limit: int=60):
    rows=kuzu_rows(_conn.execute(
        "MATCH (e:Entry) RETURN e.id,e.ts,e.narrative,e.raw_text,e.archivist_note"
        " ORDER BY e.ts DESC LIMIT $l",{"l":limit}))
    return [{"id":r[0],"ts":r[1],"narrative":r[2],"raw":r[3],"archivist_note":r[4]} for r in rows]

@app.post("/diary/{eid}/delete")
def delete_entry(eid: str):
    try: _conn.execute("MATCH (e:Entry)-[r:MENTIONS]->() WHERE e.id=$id DELETE r",{"id":eid})
    except: pass
    try: _conn.execute("MATCH (e:Entry) WHERE e.id=$id DELETE e",{"id":eid})
    except: pass
    return {"deleted":eid}

@app.get("/entities")
def entities(type: str=""):
    if type:
        rows=kuzu_rows(_conn.execute(
            "MATCH (e:Entity) WHERE e.type=$t RETURN e.id,e.name,e.type,e.summary,e.tags ORDER BY e.name",{"t":type}))
    else:
        rows=kuzu_rows(_conn.execute(
            "MATCH (e:Entity) RETURN e.id,e.name,e.type,e.summary,e.tags ORDER BY e.type,e.name"))
    return [{"id":r[0],"name":r[1],"type":r[2],"summary":r[3],
             "tags":json.loads(r[4]) if r[4] else []} for r in rows]

@app.get("/entity/{name}")
def entity_card(name: str):
    eid=slug(name)
    base=kuzu_rows(_conn.execute(
        "MATCH (e:Entity) WHERE e.id=$id RETURN e.name,e.type,e.summary,e.tags",{"id":eid}))
    if not base: raise HTTPException(404,"Not found")
    out=kuzu_rows(_conn.execute(
        "MATCH (a:Entity)-[r:LINKED]->(b:Entity) WHERE a.id=$id RETURN a.name,r.label,b.name",{"id":eid}))
    inp=kuzu_rows(_conn.execute(
        "MATCH (a:Entity)-[r:LINKED]->(b:Entity) WHERE b.id=$id RETURN a.name,r.label,b.name",{"id":eid}))
    ment=kuzu_rows(_conn.execute(
        "MATCH (en:Entry)-[:MENTIONS]->(et:Entity) WHERE et.id=$id"
        " RETURN en.ts,en.narrative,en.archivist_note ORDER BY en.ts DESC LIMIT 5",{"id":eid}))
    return {"name":base[0][0],"type":base[0][1],"summary":base[0][2],
            "tags":json.loads(base[0][3]) if base[0][3] else [],
            "links_out":[{"from":r[0],"label":r[1],"to":r[2]} for r in out],
            "links_in": [{"from":r[0],"label":r[1],"to":r[2]} for r in inp],
            "mentions":  [{"ts":r[0],"narrative":r[1],"archivist_note":r[2]} for r in ment]}

@app.get("/graph")
def graph():
    rows=kuzu_rows(_conn.execute("MATCH (a:Entity)-[r:LINKED]->(b:Entity) RETURN a.name,r.label,b.name"))
    return [{"from":r[0],"label":r[1],"to":r[2]} for r in rows]

@app.get("/missions")
def get_missions():
    rows=kuzu_rows(_conn.execute(
        "MATCH (m:Mission) RETURN m.id,m.title,m.description,m.status,m.ts,m.lore ORDER BY m.ts"))
    result=[]
    for r in rows:
        tasks=kuzu_rows(_conn.execute(
            "MATCH (t:Task) WHERE t.mission_id=$mid "
            "RETURN t.id,t.title,t.status,t.ts,"
            "t.task_type,t.reset_hours,t.required_iters,t.current_iters,"
            "t.last_reset_ts,t.streak,t.best_streak ORDER BY t.ts",
            {"mid":r[0]}))
        task_list=[]
        for t in tasks:
            td={"id":t[0],"title":t[1],"status":t[2],"ts":t[3],
                "task_type":t[4] or "once","reset_hours":int(t[5] or 24),
                "required_iters":int(t[6] or 1),"current_iters":int(t[7] or 0),
                "last_reset_ts":t[8] or "","streak":int(t[9] or 0),"best_streak":int(t[10] or 0)}
            td=_maybe_reset_task(td)
            task_list.append(td)
        result.append({"id":r[0],"title":r[1],"description":r[2],"status":r[3],"ts":r[4],
                        "lore":r[5] or "","tasks":task_list})
    return result

@app.post("/missions")
def add_mission(req: MissionReq):
    mid=str(uuid.uuid4())
    _conn.execute("CREATE (:Mission {id:$id,title:$t,description:$d,status:'active',ts:$ts})",
                  {"id":mid,"t":req.title,"d":req.description,
                   "ts":datetime.now().strftime("%Y-%m-%d %H:%M")})
    return {"id":mid,"title":req.title,"status":"active","tasks":[]}

@app.post("/missions/{mid}/complete")
def complete_mission(mid: str):
    _conn.execute("MATCH (m:Mission) WHERE m.id=$id SET m.status='done'",{"id":mid})
    return {"ok":True}

@app.post("/missions/{mid}/delete")
def delete_mission(mid: str):
    try: _conn.execute("MATCH (t:Task) WHERE t.mission_id=$id DELETE t",{"id":mid})
    except: pass
    try: _conn.execute("MATCH (m:Mission) WHERE m.id=$id DELETE m",{"id":mid})
    except: pass
    return {"deleted":mid}

@app.post("/tasks")
def add_task(req: TaskReq):
    tid=str(uuid.uuid4()); ts=datetime.now().strftime("%Y-%m-%d %H:%M")
    init_reset = ts if req.task_type == "repeat" else ""
    _conn.execute(
        "CREATE (:Task {id:$id,mission_id:$mid,title:$t,status:'active',ts:$ts,entry_id:'',"
        "task_type:$tt,reset_hours:$rh,required_iters:$ri,"
        "current_iters:0,last_reset_ts:$lr,streak:0,best_streak:0})",
        {"id":tid,"mid":req.mission_id,"t":req.title,"ts":ts,
         "tt":req.task_type,"rh":req.reset_hours,"ri":req.required_iters,"lr":init_reset})
    return {"id":tid,"title":req.title,"status":"active","task_type":req.task_type}

class TaskParamsReq(BaseModel):
    task_type: str = "once"; reset_hours: int = 24; required_iters: int = 1

@app.post("/tasks/{tid}/set-params")
def set_task_params(tid: str, req: TaskParamsReq):
    _conn.execute(
        "MATCH (t:Task) WHERE t.id=$id SET t.task_type=$tt,t.reset_hours=$rh,t.required_iters=$ri",
        {"id":tid,"tt":req.task_type,"rh":req.reset_hours,"ri":req.required_iters})
    return {"ok":True}

@app.post("/tasks/{tid}/tick")
def tick_task(tid: str):
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id "
        "RETURN t.current_iters,t.required_iters,t.last_reset_ts",{"id":tid}))
    if not rows: return {"error":"not found"}
    cur=int(rows[0][0] or 0); req=int(rows[0][1] or 1); lr=rows[0][2] or ""
    new_cur=min(cur+1,req)
    now_s=datetime.now().strftime("%Y-%m-%d %H:%M")
    if not lr: lr=now_s
    cts=now_s if new_cur>=req else ""
    _conn.execute("MATCH (t:Task) WHERE t.id=$id SET t.current_iters=$c,t.last_reset_ts=$lr,t.completed_ts=$cts",
                  {"id":tid,"c":new_cur,"lr":lr,"cts":cts})
    return {"current":new_cur,"required":req,"completed":new_cur>=req}

@app.post("/tasks/{tid}/complete")
def complete_task(tid: str):
    now_s=datetime.now().strftime("%Y-%m-%d %H:%M")
    _conn.execute("MATCH (t:Task) WHERE t.id=$id SET t.status='done',t.completed_ts=$ts",
                  {"id":tid,"ts":now_s})
    return {"ok":True}

@app.post("/tasks/{tid}/delete")
def delete_task(tid: str):
    try: _conn.execute("MATCH (t:Task) WHERE t.id=$id DELETE t",{"id":tid})
    except: pass
    return {"deleted":tid}

@app.get("/tasks/completed-today")
def completed_today():
    today=datetime.now().strftime("%Y-%m-%d")
    # Completed once-tasks
    r1=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.completed_ts STARTS WITH $d RETURN count(t)",{"d":today}))
    done=int(r1[0][0]) if r1 else 0
    # Repeat tasks ticked today (any progress, even partial) — exclude already counted
    r2=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.task_type='repeat' AND t.last_reset_ts STARTS WITH $d "
        "AND t.current_iters > 0 AND (t.completed_ts IS NULL OR NOT t.completed_ts STARTS WITH $d) "
        "RETURN count(t)",{"d":today}))
    partial=int(r2[0][0]) if r2 else 0
    return {"count": done+partial}

@app.post("/entities/{eid}/delete")
def delete_entity(eid: str):
    es=slug(eid)
    try: _conn.execute("MATCH (e:Entity)-[r:LINKED]-() WHERE e.id=$id DELETE r",{"id":es})
    except: pass
    try: _conn.execute("MATCH ()-[r:LINKED]->(e:Entity) WHERE e.id=$id DELETE r",{"id":es})
    except: pass
    try: _conn.execute("MATCH ()-[r:MENTIONS]->(e:Entity) WHERE e.id=$id DELETE r",{"id":es})
    except: pass
    try: _conn.execute("MATCH (e:Entity) WHERE e.id=$id DELETE e",{"id":es})
    except: pass
    return {"deleted":es}

class MissionUpdateReq(BaseModel): title: str = ""; description: str = ""
class TaskUpdateReq(BaseModel): title: str = ""

@app.post("/missions/{mid}/update")
def update_mission(mid: str, req: MissionUpdateReq):
    if req.title:
        _conn.execute("MATCH (m:Mission) WHERE m.id=$id SET m.title=$t, m.description=$d",
                      {"id":mid,"t":req.title,"d":req.description})
    return {"ok":True}

class MissionLoreReq(BaseModel): lore: str

@app.post("/missions/{mid}/lore")
def set_mission_lore(mid: str, req: MissionLoreReq):
    _conn.execute("MATCH (m:Mission) WHERE m.id=$id SET m.lore=$l",{"id":mid,"l":req.lore})
    return {"ok":True}

@app.post("/tasks/{tid}/update")
def update_task(tid: str, req: TaskUpdateReq):
    if req.title:
        _conn.execute("MATCH (t:Task) WHERE t.id=$id SET t.title=$t",{"id":tid,"t":req.title})
    return {"ok":True}

_REANALYZE_PROMPT = """Ты — Архивариус. Обнови лор каждого активного Пути Героя в стиле летописи Морровинда.
Лор — 1-2 лаконичных нарративных предложения: что этот Путь значит для Героя, конкретно.

АКТИВНЫЕ ПУТИ:
{missions}

ПОСЛЕДНИЕ ЗАПИСИ:
{entries}

Верни ТОЛЬКО валидный JSON без markdown:
{{"missions":[{{"id":"...","lore":"..."}}]}}"""

def _has_any_ai() -> bool:
    cfg = _app_cfg()
    return bool(_get_api_key() or cfg.get("gigachat_key"))

def _call_any_ai(prompt_text: str) -> str:
    """Try Anthropic then GigaChat, return raw text."""
    ant_key = _get_api_key()
    if ant_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ant_key)
            msg = client.messages.create(model="claude-haiku-4-5", max_tokens=1024,
                messages=[{"role":"user","content":prompt_text}])
            return msg.content[0].text if msg.content else ""
        except Exception as e: print(f"[anthropic_any] {e}")
    return _call_gigachat(prompt_text)

def _run_reanalyze_bg():
    try:
        if not _has_any_ai(): return
        miss=kuzu_rows(_conn.execute(
            "MATCH (m:Mission) WHERE m.status='active' RETURN m.id,m.title ORDER BY m.ts"))
        entries=kuzu_rows(_conn.execute(
            "MATCH (e:Entry) RETURN e.ts,e.narrative ORDER BY e.ts DESC LIMIT 10"))
        miss_txt="\n".join(f"ID={r[0]}: {r[1]}" for r in miss)
        ent_txt="\n".join(f"[{r[0]}] {r[1]}" for r in entries)
        p=_REANALYZE_PROMPT.format(missions=miss_txt or "нет", entries=ent_txt or "нет")
        text = _call_any_ai(p)
        m=re.search(r'\{.*\}',text,re.DOTALL)
        if m:
            data=json.loads(m.group())
            for item in data.get("missions",[]):
                lore=item.get("lore","").strip()
                if lore and item.get("id"):
                    try: _conn.execute("MATCH (m:Mission) WHERE m.id=$id SET m.lore=$l",
                                       {"id":item["id"],"l":lore})
                    except: pass
    except Exception as e: print(f"[reanalyze_bg] {e}")
    finally:
        try:
            inbox=json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
            inbox=[i for i in inbox if i.get("type")!="reanalyze"]
            INBOX_FILE.write_text(json.dumps(inbox,ensure_ascii=False,indent=2))
        except: pass

@app.post("/reanalyze")
def do_reanalyze():
    if _has_any_ai():
        try:
            inbox=json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
            inbox=[i for i in inbox if i.get("type")!="reanalyze"]
            rid="reanalyze_"+datetime.now().strftime("%Y%m%d_%H%M%S")
            inbox.append({"id":rid,"type":"reanalyze","ts":datetime.now().strftime("%Y-%m-%d %H:%M")})
            INBOX_FILE.write_text(json.dumps(inbox,ensure_ascii=False,indent=2))
        except: pass
        threading.Thread(target=_run_reanalyze_bg,daemon=True).start()
        return {"ok":True,"status":"processing"}
    else:
        return {"ok":False,"status":"no_api_key"}

class GigaChatKeyReq(BaseModel): gigachat_key: str; gigachat_scope: str = "GIGACHAT_API_PERS"

@app.post("/config/gigachat")
def save_gigachat(req: GigaChatKeyReq):
    cfg=_app_cfg()
    cfg["gigachat_key"]=req.gigachat_key.strip()
    cfg["gigachat_scope"]=req.gigachat_scope.strip()
    APP_CFG_FILE.write_text(json.dumps(cfg,ensure_ascii=False))
    return {"ok":True}

@app.get("/reanalyze/status")
def reanalyze_status():
    try:
        inbox=json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
        running=any(i.get("type")=="reanalyze" for i in inbox)
    except: running=False
    return {"running": running, "has_key": bool(_get_api_key())}

class ApiKeyReq(BaseModel): api_key: str

@app.post("/config/api-key")
def save_api_key(req: ApiKeyReq):
    cfg=_app_cfg(); cfg["api_key"]=req.api_key.strip()
    APP_CFG_FILE.write_text(json.dumps(cfg,ensure_ascii=False))
    return {"ok":True,"has_key":bool(cfg["api_key"])}

@app.get("/config/status")
def config_status():
    cfg=_app_cfg()
    return {"has_key": bool(_get_api_key()), "has_gigachat": bool(cfg.get("gigachat_key")),
            "active": "anthropic" if _get_api_key() else ("gigachat" if cfg.get("gigachat_key") else "none")}

@app.get("/export")
def export_data():
    entries=kuzu_rows(_conn.execute("MATCH (e:Entry) RETURN e.id,e.ts,e.raw_text,e.narrative,e.archivist_note"))
    entities=kuzu_rows(_conn.execute("MATCH (e:Entity) RETURN e.id,e.name,e.type,e.summary,e.tags"))
    missions=kuzu_rows(_conn.execute("MATCH (m:Mission) RETURN m.id,m.title,m.description,m.status,m.ts,m.lore"))
    tasks=kuzu_rows(_conn.execute("MATCH (t:Task) RETURN t.id,t.mission_id,t.title,t.status,t.ts,t.task_type,t.reset_hours,t.required_iters,t.current_iters,t.last_reset_ts,t.streak,t.best_streak"))
    finances=kuzu_rows(_conn.execute("MATCH (f:Finance) RETURN f.id,f.amount,f.direction,f.category,f.note,f.ts"))
    return {
        "entries":[{"id":r[0],"ts":r[1],"raw_text":r[2],"narrative":r[3],"archivist_note":r[4]} for r in entries],
        "entities":[{"id":r[0],"name":r[1],"type":r[2],"summary":r[3],"tags":r[4]} for r in entities],
        "missions":[{"id":r[0],"title":r[1],"description":r[2],"status":r[3],"ts":r[4],"lore":r[5] or ""} for r in missions],
        "tasks":[{"id":r[0],"mission_id":r[1],"title":r[2],"status":r[3],"ts":r[4],"task_type":r[5] or "once","reset_hours":r[6] or 24,"required_iters":r[7] or 1,"current_iters":r[8] or 0,"last_reset_ts":r[9] or "","streak":r[10] or 0,"best_streak":r[11] or 0} for r in tasks],
        "finances":[{"id":r[0],"amount":r[1],"direction":r[2],"category":r[3],"note":r[4],"ts":r[5]} for r in finances],
    }

@app.post("/import")
def import_data(data: dict):
    for r in data.get("entries",[]):
        try: _conn.execute("CREATE (:Entry {id:$id,ts:$ts,raw_text:$rt,narrative:$n,archivist_note:$an})",{"id":r["id"],"ts":r["ts"],"rt":r.get("raw_text",""),"n":r.get("narrative",""),"an":r.get("archivist_note","")})
        except: pass
    for r in data.get("entities",[]):
        try: _conn.execute("CREATE (:Entity {id:$id,name:$n,type:$t,summary:$s,tags:$tg})",{"id":r["id"],"n":r["name"],"t":r["type"],"s":r.get("summary",""),"tg":r.get("tags","")})
        except: pass
    for r in data.get("missions",[]):
        try: _conn.execute("CREATE (:Mission {id:$id,title:$t,description:$d,status:$s,ts:$ts,lore:$l})",{"id":r["id"],"t":r["title"],"d":r.get("description",""),"s":r["status"],"ts":r["ts"],"l":r.get("lore","")})
        except: pass
    for r in data.get("tasks",[]):
        try: _conn.execute("CREATE (:Task {id:$id,mission_id:$mid,title:$t,status:$s,ts:$ts,entry_id:'',task_type:$tt,reset_hours:$rh,required_iters:$ri,current_iters:$ci,last_reset_ts:$lr,streak:$st,best_streak:$bs,completed_ts:''})",{"id":r["id"],"mid":r["mission_id"],"t":r["title"],"s":r["status"],"ts":r["ts"],"tt":r.get("task_type","once"),"rh":r.get("reset_hours",24),"ri":r.get("required_iters",1),"ci":r.get("current_iters",0),"lr":r.get("last_reset_ts",""),"st":r.get("streak",0),"bs":r.get("best_streak",0)})
        except: pass
    for r in data.get("finances",[]):
        try: _conn.execute("CREATE (:Finance {id:$id,amount:$a,direction:$d,category:$c,note:$n,ts:$ts})",{"id":r["id"],"a":r["amount"],"d":r["direction"],"c":r.get("category",""),"n":r.get("note",""),"ts":r["ts"]})
        except: pass
    return {"ok":True}

@app.get("/finances")
def get_finances():
    rows=kuzu_rows(_conn.execute(
        "MATCH (f:Finance) RETURN f.id,f.amount,f.direction,f.category,f.note,f.ts"
        " ORDER BY f.ts DESC LIMIT 60"))
    items=[{"id":r[0],"amount":r[1],"direction":r[2],"category":r[3],"note":r[4],"ts":r[5]} for r in rows]
    inc=sum(i["amount"] for i in items if i["direction"]=="доход")
    exp=sum(i["amount"] for i in items if i["direction"]=="расход")
    return {"balance":inc-exp,"income":inc,"expense":exp,"items":items}

@app.post("/finances")
def add_finance(req: FinanceReq):
    fid=str(uuid.uuid4())
    _conn.execute(
        "CREATE (:Finance {id:$id,amount:$a,direction:$d,category:$c,note:$n,ts:$ts})",
        {"id":fid,"a":req.amount,"d":req.direction,"c":req.category,
         "n":req.note,"ts":datetime.now().strftime("%Y-%m-%d %H:%M")})
    return {"id":fid}

# ── Pocket ───────────────────────────────────────────────────────────────────
POCKET_CFG = Path(__file__).parent / "pocket_config.json"

def _pocket_cfg():
    if POCKET_CFG.exists():
        try: return json.loads(POCKET_CFG.read_text())
        except: pass
    return {"reserve_pct": 20}

class PocketIncomeReq(BaseModel): amount: float; source: str = ""
class PocketExpenseReq(BaseModel): amount: float; note: str = ""; from_deferred: bool = False
class PocketCfgReq(BaseModel): reserve_pct: int

@app.get("/pocket")
def get_pocket():
    cfg=_pocket_cfg()
    rows=kuzu_rows(_conn.execute(
        "MATCH (f:Finance) WHERE f.category='pocket' "
        "RETURN f.id,f.amount,f.direction,f.note,f.ts ORDER BY f.ts DESC"))
    items=[{"id":r[0],"amount":float(r[1]),"direction":r[2],"note":r[3],"ts":r[4]} for r in rows]
    balance=sum(i["amount"] for i in items if i["direction"]=="p_income")
    balance-=sum(i["amount"] for i in items if i["direction"]=="p_expense")
    balance+=sum(i["amount"] for i in items if i["direction"]=="p_adjust")
    deferred=sum(i["amount"] for i in items if i["direction"]=="p_deferred")
    deferred-=sum(i["amount"] for i in items if i["direction"]=="p_deferred_spend")
    deferred+=sum(i["amount"] for i in items if i["direction"]=="p_deferred_adjust")
    return {"balance":round(balance,2),"deferred":round(deferred,2),
            "reserve_pct":cfg["reserve_pct"],"transactions":items[:40]}

@app.post("/pocket/income")
def pocket_income(req: PocketIncomeReq):
    cfg=_pocket_cfg(); pct=cfg["reserve_pct"]/100
    ts=datetime.now().strftime("%Y-%m-%d %H:%M")
    deferred=round(req.amount*pct,2); spendable=round(req.amount-deferred,2)
    for d,a,n in [("p_income",spendable,req.source or "пополнение"),
                  ("p_deferred",deferred,f"резерв {cfg['reserve_pct']}% от {req.amount}")]:
        _conn.execute(
            "CREATE (:Finance {id:$id,amount:$a,direction:$d,category:'pocket',note:$n,ts:$ts})",
            {"id":str(uuid.uuid4()),"a":a,"d":d,"n":n,"ts":ts})
    return {"ok":True,"spendable":spendable,"deferred":deferred}

@app.post("/pocket/expense")
def pocket_expense(req: PocketExpenseReq):
    direction="p_deferred_spend" if req.from_deferred else "p_expense"
    _conn.execute(
        "CREATE (:Finance {id:$id,amount:$a,direction:$d,category:'pocket',note:$n,ts:$ts})",
        {"id":str(uuid.uuid4()),"a":req.amount,"d":direction,
         "n":req.note or "расход","ts":datetime.now().strftime("%Y-%m-%d %H:%M")})
    return {"ok":True}

@app.post("/pocket/config")
def pocket_config(req: PocketCfgReq):
    cfg=_pocket_cfg(); cfg["reserve_pct"]=max(0,min(99,req.reserve_pct))
    POCKET_CFG.write_text(json.dumps(cfg,ensure_ascii=False))
    return {"ok":True,"reserve_pct":cfg["reserve_pct"]}

class PocketAdjustReq(BaseModel): amount: float; note: str = ""; target: str = "balance"

@app.post("/pocket/adjust")
def pocket_adjust(req: PocketAdjustReq):
    direction = "p_adjust" if req.target == "balance" else "p_deferred_adjust"
    note = req.note or ("корректировка баланса" if req.target == "balance" else "корректировка резерва")
    _conn.execute(
        "CREATE (:Finance {id:$id,amount:$a,direction:$d,category:'pocket',note:$n,ts:$ts})",
        {"id":str(uuid.uuid4()),"a":req.amount,"d":direction,"n":note,
         "ts":datetime.now().strftime("%Y-%m-%d %H:%M")})
    return {"ok":True}

@app.get("/modes")
def get_modes():
    rows=kuzu_rows(_conn.execute(
        "MATCH (m:Mode) RETURN m.id,m.name,m.description,m.active,m.started_ts ORDER BY m.started_ts DESC"))
    return [{"id":r[0],"name":r[1],"description":r[2],"active":r[3]=="true","started_ts":r[4]} for r in rows]

@app.post("/modes")
def add_mode(req: ModeReq):
    mid=str(uuid.uuid4())
    _conn.execute(
        "CREATE (:Mode {id:$id,name:$n,description:$d,active:'true',started_ts:$ts})",
        {"id":mid,"n":req.name,"d":req.description,"ts":datetime.now().strftime("%Y-%m-%d %H:%M")})
    return {"id":mid,"name":req.name,"active":True}

@app.post("/modes/{mid}/toggle")
def toggle_mode(mid: str):
    rows=kuzu_rows(_conn.execute("MATCH (m:Mode) WHERE m.id=$id RETURN m.active",{"id":mid}))
    if rows:
        new="false" if rows[0][0]=="true" else "true"
        _conn.execute("MATCH (m:Mode) WHERE m.id=$id SET m.active=$a",{"id":mid,"a":new})
    return {"ok":True}

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Life RPG</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --nav-bg:#1a1208; --nav-text:#c8a878; --nav-dim:#5a3e1a; --nav-border:#2d1e08;
  --page:#ede3c8; --paper:#fdf6e3; --paper2:#f5ecd0;
  --ink:#2d1e08; --ink2:#5c3d18; --ink3:#8a6040;
  --red:#8b2e0f; --blue:#1a4a6b; --gold:#8b6914; --green:#2d5c14;
  --border:#c4a070; --border2:#dfc898; --shadow:rgba(45,30,8,.13);
}
body{background:var(--page);color:var(--ink);font-family:'Georgia',serif;
  display:grid;grid-template-columns:200px 1fr;grid-template-rows:auto 1fr auto;
  height:100vh;overflow:hidden}

/* ── NAV ── */
nav{grid-column:1;grid-row:1/4;background:var(--nav-bg);border-right:2px solid #0e0906;
  display:flex;flex-direction:column;overflow-y:auto}
.nav-logo{padding:18px 14px 16px;border-bottom:1px solid var(--nav-border)}
.nav-game{font-size:19px;color:#f0d890;letter-spacing:3px;margin-bottom:3px}
.nav-tagline{font-size:9px;color:var(--nav-dim);font-family:sans-serif;letter-spacing:1.5px;
  text-transform:uppercase;line-height:1.6}
.nav-item{padding:11px 14px;cursor:pointer;font-family:sans-serif;font-size:13px;
  color:var(--nav-dim);display:flex;align-items:center;gap:9px;
  border-left:2px solid transparent;transition:all .12s}
.nav-item:hover{color:var(--nav-text);background:rgba(255,255,255,.03)}
.nav-item.active{color:#f0d890;border-left-color:var(--gold);background:rgba(255,255,255,.055)}
.nav-sep{border-top:1px solid var(--nav-border);margin:4px 0}
.nav-bottom{margin-top:auto;padding:12px 14px;border-top:1px solid var(--nav-border);
  font-size:10px;color:var(--nav-dim);font-family:sans-serif;line-height:1.7}

/* ── HEADER ── */
header{grid-column:2;grid-row:1;background:var(--paper2);border-bottom:2px solid var(--border);
  padding:10px 22px;display:flex;align-items:center;justify-content:space-between;
  box-shadow:0 2px 8px var(--shadow)}
.hdr-title{font-size:12px;color:var(--ink3);font-family:sans-serif;letter-spacing:4px;
  text-transform:uppercase}
.hdr-date{font-size:12px;color:var(--ink3);font-family:sans-serif}

/* ── MAIN ── */
main{grid-column:2;grid-row:2;overflow:hidden;position:relative}
section{display:none;height:100%;overflow-y:auto}
section.active{display:block}

/* ── INPUT BAR ── */
#input-bar{grid-column:2;grid-row:3;background:var(--paper2);border-top:2px solid var(--border);
  padding:12px 24px;display:flex;gap:12px;align-items:flex-end;
  box-shadow:0 -2px 8px var(--shadow)}
#txt{flex:1;background:var(--paper);border:1px solid var(--border);color:var(--ink);
  font-family:'Georgia',serif;font-size:15px;padding:11px 16px;border-radius:4px;
  resize:none;height:60px;outline:none;line-height:1.55}
#txt:focus{border-color:var(--gold);box-shadow:0 0 0 2px rgba(139,105,20,.12)}
#txt::placeholder{color:var(--border)}
#send-btn{background:var(--gold);border:none;color:#fff;font-family:'Georgia',serif;
  font-size:14px;padding:0 28px;height:60px;border-radius:4px;cursor:pointer;
  white-space:nowrap;letter-spacing:.4px}
#send-btn:hover{background:#a07820}
#send-btn:disabled{background:var(--border2);color:var(--ink3);cursor:not-allowed}
/* Settings modal */
#settings-modal{display:none;position:fixed;inset:0;background:rgba(20,12,4,.65);z-index:500;
  align-items:center;justify-content:center}
#settings-modal.open{display:flex}
#settings-box{background:var(--paper);border:2px solid var(--border);border-radius:4px;
  padding:28px 32px;min-width:360px;max-width:480px}
.settings-title{font-size:16px;color:var(--ink);margin-bottom:18px}
.settings-label{font-size:11px;font-family:sans-serif;color:var(--ink3);
  letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}
.settings-input{width:100%;box-sizing:border-box;background:var(--paper2);
  border:1px solid var(--border);color:var(--ink);font-family:'Courier New',monospace;
  font-size:12px;padding:8px 11px;border-radius:3px;outline:none;margin-bottom:12px}
.settings-input:focus{border-color:var(--gold)}
.settings-hint{font-size:11px;font-family:sans-serif;color:var(--ink3);margin-bottom:16px;line-height:1.6}
.settings-status{font-size:12px;font-family:sans-serif;margin-bottom:12px}
.settings-status.ok{color:var(--green)} .settings-status.missing{color:var(--red)}

/* ── JOURNAL ── */
#s-journal{padding:0}
.journal-wrap{display:grid;grid-template-columns:1fr 230px;height:100%;overflow:hidden}
.journal-main{overflow-y:auto;padding:40px 56px 60px;background:var(--paper)}
.journal-aside{overflow-y:auto;padding:20px 16px;background:var(--paper2);
  border-left:1px solid var(--border)}
.day-block{margin-bottom:0}
.day-heading{font-size:11px;color:var(--red);font-family:sans-serif;letter-spacing:3px;
  text-transform:uppercase;font-weight:700;margin:44px 0 4px;
  display:flex;align-items:center;gap:12px}
.day-heading::after{content:'';flex:1;height:1px;
  background:linear-gradient(to right,var(--border),transparent)}
.day-sub{font-size:10px;color:var(--ink3);font-family:sans-serif;letter-spacing:1.5px;
  margin-bottom:20px;font-style:italic}
.day-block:first-child .day-heading{margin-top:4px}
.entry{margin-bottom:24px}
.entry-text{font-size:15px;line-height:1.95;color:var(--ink);text-align:justify;hyphens:auto}
.entry+.entry::before{content:'✦';display:block;text-align:center;color:var(--border2);
  font-size:9px;margin-bottom:24px;letter-spacing:10px}
.entry-raw{font-size:12px;color:var(--ink3);font-style:italic;margin-top:10px;
  padding:8px 0 0 14px;border-left:2px solid var(--border2);line-height:1.6}
.entry-archivist{font-size:11px;color:var(--gold);font-family:sans-serif;font-style:italic;
  margin-top:10px;padding:6px 10px;border-left:2px solid rgba(139,105,20,.4);
  background:rgba(139,105,20,.04);line-height:1.6}
.entry-time{font-size:10px;color:var(--ink3);font-family:sans-serif;
  margin-top:6px;text-align:right;opacity:.65}
.ent-link{cursor:pointer;border-bottom:1px dotted currentColor;transition:opacity .1s}
.ent-link:hover{opacity:.7}
.ent-link.type-person{color:var(--blue)}
.ent-link.type-project{color:var(--green)}
.ent-link.type-concept{color:#7b4fa0}
.ent-link.type-event{color:var(--gold)}
.ent-link.type-place{color:#6b4a22}
.aside-section{margin-bottom:20px}
.aside-label{font-size:9px;letter-spacing:3px;color:var(--ink3);font-family:sans-serif;
  text-transform:uppercase;margin-bottom:8px;padding-bottom:6px;
  border-bottom:1px solid var(--border2)}
.aside-mission{padding:7px 10px;border:1px solid var(--border2);border-radius:3px;
  margin-bottom:5px;cursor:pointer;background:var(--paper);transition:border-color .12s}
.aside-mission:hover{border-color:var(--gold)}
.aside-mission-t{font-size:13px;color:var(--ink)}
.aside-entity{display:flex;align-items:center;gap:8px;padding:6px 0;cursor:pointer;
  border-bottom:.5px solid var(--border2)}
.aside-entity:last-child{border-bottom:none}
.aside-entity:hover .aside-entity-name{color:var(--blue)}
.aside-entity-ic{font-size:14px;flex-shrink:0}
.aside-entity-name{font-size:13px;color:var(--ink)}
.aside-entity-sub{font-size:10px;color:var(--ink3);font-family:sans-serif}

/* ── MISSIONS ── */
#s-missions{padding:0}
.missions-wrap{max-width:780px;margin:0 auto;padding:40px 40px 80px}
.missions-topbar{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px}
.missions-eyebrow{font-size:10px;letter-spacing:4px;text-transform:uppercase;
  color:var(--red);font-family:sans-serif}
.missions-heading{font-size:22px;color:var(--ink);margin-bottom:4px}
.missions-sub{font-size:12px;color:var(--ink3);font-family:sans-serif;margin-bottom:36px}
.mission-lore{font-size:12px;color:var(--ink3);font-style:italic;margin-top:3px;line-height:1.5;
  font-family:'Georgia',serif;opacity:.85}
.mission-block{margin-bottom:36px;padding-bottom:32px;border-bottom:1px solid var(--border2)}
.mission-block:last-child{border-bottom:none;padding-bottom:0}
.mission-block-hdr{display:flex;align-items:flex-start;gap:10px;cursor:pointer;
  padding:2px 0;user-select:none}
.mission-star{color:var(--gold);font-size:20px;flex-shrink:0;margin-top:1px;line-height:1}
.mission-block-info{flex:1}
.mission-block-title{font-size:19px;color:var(--ink);line-height:1.3}
.mission-block.done .mission-block-title{color:var(--ink3);text-decoration:line-through}
.mission-progress-badge{display:inline-block;font-size:10px;font-family:sans-serif;
  padding:2px 10px;border-radius:10px;margin-top:4px;
  background:rgba(139,105,20,.1);color:var(--gold);border:1px solid rgba(139,105,20,.25)}
.mission-block-chevron{font-size:11px;color:var(--border);margin-top:6px;flex-shrink:0;
  transition:transform .2s}
.mission-block-chevron.open{transform:rotate(180deg)}
.mission-desc-text{font-size:13px;color:var(--ink3);font-family:sans-serif;
  line-height:1.65;margin:10px 0 0 30px}
.quest-chain{margin:16px 0 0 28px;padding-left:16px;
  border-left:1.5px solid var(--border2);display:none}
.quest-chain.open{display:block}
.quest-item{display:flex;align-items:flex-start;gap:10px;padding:9px 0;
  border-bottom:.5px solid rgba(200,164,122,.25)}
.quest-item:last-child{border-bottom:none}
.quest-cb{width:16px;height:16px;border:1.5px solid var(--border);border-radius:2px;
  flex-shrink:0;margin-top:2px;cursor:pointer;display:flex;align-items:center;
  justify-content:center;font-size:9px;color:var(--green);transition:all .15s}
.quest-cb:hover{border-color:var(--gold)}
.quest-cb.done{background:var(--green);border-color:var(--green);color:#fff}
.quest-info{flex:1}
.quest-title{font-size:14px;color:var(--ink);line-height:1.45}
.quest-title.done{text-decoration:line-through;color:var(--ink3)}
.quest-ts{font-size:10px;color:var(--ink3);font-family:sans-serif;margin-top:2px}
.quest-del{background:none;border:none;color:var(--border2);cursor:pointer;
  font-size:14px;flex-shrink:0;padding:2px;transition:color .12s}
.quest-del:hover{color:var(--red)}
.archivist-warning{margin:12px 0;padding:12px 14px;
  background:rgba(139,46,15,.05);border-left:2px solid rgba(139,46,15,.4);
  font-size:12px;color:var(--red);font-family:sans-serif;line-height:1.7;font-style:italic}
.archivist-wisdom{margin:12px 0;padding:10px 14px;
  background:rgba(139,105,20,.05);border-left:2px solid rgba(139,105,20,.3);
  font-size:12px;color:var(--ink2);font-family:sans-serif;line-height:1.7;font-style:italic}
.quest-add-row{padding:12px 0 0}
.btn-quest-add{background:none;border:1px dashed var(--border2);color:var(--ink3);
  font-family:sans-serif;font-size:12px;padding:5px 14px;border-radius:3px;
  cursor:pointer;transition:all .15s}
.btn-quest-add:hover{border-color:var(--gold);color:var(--gold)}
.mission-actions{margin-top:14px;margin-left:28px;display:flex;gap:8px;flex-wrap:wrap}

/* ── BASE (знаний) ── */
#s-base{padding:0}
.base-wrap{padding:36px 40px 60px;overflow-y:auto;height:100%}
.base-topbar{display:flex;align-items:center;justify-content:space-between;
  margin-bottom:28px;padding-bottom:16px;border-bottom:2px solid var(--border)}
.base-heading{font-size:21px;color:var(--ink)}
.base-topbar-right{display:flex;gap:10px;align-items:center}
.btn-reanalyze{background:var(--red);border:none;color:#fff;font-family:'Georgia',serif;
  font-size:13px;padding:7px 18px;border-radius:3px;cursor:pointer;letter-spacing:.4px}
.btn-reanalyze:hover{background:#a03010}
.base-section{margin-bottom:40px}
.base-sec-hdr{display:flex;align-items:baseline;gap:10px;margin-bottom:14px;
  padding-bottom:8px;border-bottom:1.5px solid var(--border2)}
.base-sec-title{font-size:12px;letter-spacing:3px;font-family:sans-serif;
  text-transform:uppercase;font-weight:700}
.base-sec-count{font-size:11px;color:var(--ink3);font-family:sans-serif}
.base-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px}
.bcard{background:var(--paper);border:1px solid var(--border2);border-radius:3px;
  padding:18px 18px 14px 22px;cursor:pointer;transition:all .15s;
  box-shadow:1px 2px 6px var(--shadow);position:relative;overflow:hidden}
.bcard:hover{border-color:var(--gold);box-shadow:3px 4px 14px var(--shadow);transform:translateY(-1px)}
.bcard-stripe{position:absolute;left:0;top:0;bottom:0;width:3px}
.bcard-name{font-size:16px;color:var(--ink);margin-bottom:2px}
.bcard-type{font-size:9px;letter-spacing:2.5px;font-family:sans-serif;
  text-transform:uppercase;margin-bottom:10px;font-weight:600}
.bcard-summary{font-size:12px;color:var(--ink2);font-family:sans-serif;
  line-height:1.6;margin-bottom:10px}
.bcard-rels{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px}
.bcard-rel{font-size:10px;font-family:sans-serif;padding:2px 8px;border-radius:10px;
  background:rgba(139,105,20,.08);color:var(--gold);border:1px solid rgba(139,105,20,.2);
  cursor:pointer;transition:background .12s}
.bcard-rel:hover{background:rgba(139,105,20,.18)}
.bcard-tags{display:flex;flex-wrap:wrap;gap:3px;margin-top:4px}
.bcard-tag{font-size:9px;font-family:sans-serif;padding:1px 7px;border-radius:8px;
  background:var(--border2);color:var(--ink3)}
.bcard-footer{font-size:10px;color:var(--ink3);font-family:sans-serif;
  padding-top:8px;border-top:1px solid var(--border2);margin-top:4px}

/* ── ENTITY MODAL ── */
#ent-modal{display:none;position:fixed;inset:0;background:rgba(20,12,4,.55);z-index:300;
  align-items:center;justify-content:center}
#ent-modal.open{display:flex}
#ent-box{background:var(--paper);border:2px solid var(--border);border-radius:4px;
  padding:28px;width:560px;max-height:84vh;overflow-y:auto;position:relative;
  box-shadow:0 10px 36px rgba(0,0,0,.3)}
#ent-close{position:absolute;top:14px;right:14px;background:none;border:none;
  font-size:22px;cursor:pointer;color:var(--ink3)}
#ent-close:hover{color:var(--ink)}
.ent-name{font-size:22px;color:var(--ink);margin-bottom:2px}
.ent-type{font-size:9px;color:var(--gold);font-family:sans-serif;letter-spacing:3px;
  text-transform:uppercase;margin-bottom:12px}
.ent-summary{font-size:13px;color:var(--ink2);line-height:1.7;margin-bottom:18px;
  padding-bottom:14px;border-bottom:1px solid var(--border2)}
.ent-tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:-10px;margin-bottom:14px}
.ent-tag{font-size:10px;font-family:sans-serif;padding:2px 8px;border-radius:8px;
  background:var(--border2);color:var(--ink3)}
.ent-sec{font-size:9px;letter-spacing:3px;color:var(--ink3);font-family:sans-serif;
  text-transform:uppercase;margin:14px 0 7px}
.rel-row{font-size:13px;color:var(--ink2);padding:5px 0;display:flex;gap:6px;
  align-items:baseline;flex-wrap:wrap;border-bottom:.5px solid var(--border2)}
.rel-row:last-child{border-bottom:none}
.rel-badge{font-size:11px;font-family:sans-serif;padding:1px 9px;border-radius:10px;
  background:rgba(139,105,20,.1);color:var(--gold);border:1px solid rgba(139,105,20,.25)}
.rel-name{cursor:pointer;color:var(--blue);border-bottom:1px dotted var(--blue)}
.rel-name:hover{opacity:.75}
.ment-ts{font-size:10px;color:var(--ink3);font-family:sans-serif;margin-top:10px;margin-bottom:3px}
.ment-text{font-size:13px;color:var(--ink2);line-height:1.6;padding-bottom:10px;
  border-bottom:.5px solid var(--border2)}
.ment-archivist{font-size:11px;color:var(--gold);font-family:sans-serif;
  font-style:italic;padding:4px 8px;background:rgba(139,105,20,.06);margin-top:4px}
.ent-del{background:none;border:1px solid rgba(139,46,15,.3);color:var(--red);
  font-family:sans-serif;font-size:11px;padding:4px 12px;border-radius:3px;cursor:pointer;
  margin-top:16px}
.ent-del:hover{background:var(--red);color:#fff}

/* ── REPEAT TASKS ── */
.quest-item.repeat-task{background:rgba(139,105,20,.04);border-left:2px solid rgba(139,105,20,.28);padding-left:10px}
.repeat-badge{font-size:9px;font-family:sans-serif;padding:1px 8px;border-radius:8px;letter-spacing:.8px;
  background:rgba(139,105,20,.1);color:var(--gold);border:1px solid rgba(139,105,20,.25);flex-shrink:0}
.repeat-progress{display:flex;align-items:center;gap:7px;margin-top:5px;flex-wrap:wrap}
.iter-btn{background:var(--gold);border:none;color:#fff;font-family:sans-serif;font-size:11px;
  padding:2px 11px;border-radius:10px;cursor:pointer;transition:background .12s;flex-shrink:0}
.iter-btn:hover{background:#a07820}
.iter-btn:disabled{background:var(--green);cursor:default}
.iter-count{font-size:13px;font-family:sans-serif;font-weight:700;color:var(--ink2)}
.iter-count.done{color:var(--green)}
.streak-display{font-size:11px;font-family:sans-serif;color:var(--gold)}
.streak-best{font-size:10px;font-family:sans-serif;color:var(--ink3)}
.reset-hint{font-size:10px;font-family:sans-serif;color:var(--ink3);opacity:.7}

/* ── INLINE EDIT ── */
.btn-edit-inline{background:none;border:none;color:var(--border);cursor:pointer;
  font-size:13px;padding:2px 6px;opacity:0;transition:opacity .15s;flex-shrink:0}
.mission-block-hdr:hover .btn-edit-inline{opacity:1}
.quest-item:hover .btn-edit-inline{opacity:1}
.btn-edit-inline:hover{color:var(--gold)}
.inline-edit-input{background:var(--paper2);border:1px solid var(--gold);color:var(--ink);
  font-family:'Georgia',serif;font-size:inherit;padding:2px 8px;border-radius:3px;
  outline:none;width:100%;box-shadow:0 0 0 2px rgba(139,105,20,.12)}

/* ── DIALOGS ── */
.dlg{display:none;position:fixed;inset:0;background:rgba(20,12,4,.5);z-index:400;
  align-items:center;justify-content:center}
.dlg.open{display:flex}
.dlg-box{background:var(--paper);border:2px solid var(--border);border-radius:4px;
  padding:26px;width:440px;box-shadow:0 8px 28px rgba(0,0,0,.2)}
.dlg-title{font-size:18px;color:var(--ink);margin-bottom:16px}
.dlg-input{width:100%;background:var(--paper2);border:1px solid var(--border);color:var(--ink);
  font-family:'Georgia',serif;font-size:14px;padding:10px 12px;border-radius:3px;
  outline:none;margin-bottom:10px}
.dlg-input:focus{border-color:var(--gold)}
.dlg-textarea{width:100%;background:var(--paper2);border:1px solid var(--border);color:var(--ink);
  font-family:'Georgia',serif;font-size:14px;padding:10px 12px;border-radius:3px;
  outline:none;resize:none;height:80px;margin-bottom:10px}
.dlg-textarea:focus{border-color:var(--gold)}
.dlg-btns{display:flex;gap:10px;justify-content:flex-end;margin-top:6px}
.btn-primary{background:var(--gold);border:none;color:#fff;font-family:'Georgia',serif;
  font-size:14px;padding:8px 24px;border-radius:3px;cursor:pointer}
.btn-primary:hover{background:#a07820}
.btn-cancel{background:none;border:1px solid var(--border);color:var(--ink3);
  font-family:'Georgia',serif;font-size:14px;padding:8px 20px;border-radius:3px;cursor:pointer}
.btn-cancel:hover{border-color:var(--ink2);color:var(--ink)}
.btn-add{background:none;border:1px solid var(--gold);color:var(--gold);
  font-family:'Georgia',serif;font-size:13px;padding:6px 18px;border-radius:3px;cursor:pointer}
.btn-add:hover{background:var(--gold);color:#fff}
.btn-sm{font-size:11px;font-family:sans-serif;padding:4px 12px;border-radius:3px;
  cursor:pointer;border:1px solid;background:none}
.btn-ok{border-color:var(--green);color:var(--green)}
.btn-ok:hover{background:var(--green);color:#fff}
.btn-danger{border-color:rgba(139,46,15,.4);color:var(--red)}
.btn-danger:hover{background:var(--red);color:#fff}
.empty{padding:48px 24px;text-align:center;color:var(--ink3);font-family:sans-serif;font-size:13px}

/* ── REANALYZE MODAL ── */
#reanalyze-modal{display:none;position:fixed;inset:0;background:rgba(20,12,4,.65);z-index:500;
  align-items:flex-start;justify-content:center;padding-top:40px}
#reanalyze-modal.open{display:flex}
#reanalyze-box{background:var(--paper);border:2px solid var(--border);border-radius:4px;
  width:680px;max-height:80vh;display:flex;flex-direction:column;
  box-shadow:0 16px 48px rgba(0,0,0,.35)}
.reanalyze-hdr{padding:20px 24px 14px;border-bottom:1px solid var(--border2);
  display:flex;align-items:center;justify-content:space-between}
.reanalyze-title{font-size:17px;color:var(--ink)}
.reanalyze-close{background:none;border:none;font-size:20px;cursor:pointer;color:var(--ink3)}
.reanalyze-body{padding:16px 24px;overflow-y:auto;flex:1}
.reanalyze-intro{font-size:13px;color:var(--ink3);font-family:sans-serif;
  line-height:1.65;margin-bottom:14px}
#reanalyze-prompt{width:100%;height:280px;background:var(--paper2);
  border:1px solid var(--border);color:var(--ink);font-family:'Courier New',monospace;
  font-size:11.5px;padding:12px;border-radius:3px;resize:none;outline:none;line-height:1.5}
.reanalyze-ftr{padding:14px 24px;border-top:1px solid var(--border2);
  display:flex;gap:10px;justify-content:flex-end}

::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:var(--page)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

/* ── POCKET ── */
#s-pocket{padding:40px 56px 60px;overflow-y:auto;background:var(--paper)}
.pocket-cards{display:grid;grid-template-columns:1fr 1fr;gap:18px;max-width:640px;margin-bottom:36px}
.pocket-card{background:var(--paper2);border:1px solid var(--border2);border-radius:4px;padding:20px 24px}
.pocket-card-label{font-size:10px;letter-spacing:3px;text-transform:uppercase;
  font-family:sans-serif;color:var(--ink3);margin-bottom:6px}
.pocket-card-amount{font-size:28px;color:var(--ink);letter-spacing:-.5px}
.pocket-card.deferred .pocket-card-amount{color:var(--gold)}
.pocket-reserve-row{display:flex;align-items:center;gap:10px;margin-bottom:30px;
  font-size:13px;font-family:sans-serif;color:var(--ink3)}
.pocket-reserve-row input{width:52px;background:var(--paper2);border:1px solid var(--border);
  color:var(--ink);font-family:sans-serif;font-size:13px;padding:4px 8px;border-radius:3px;
  text-align:center;outline:none}
.pocket-actions{display:flex;gap:12px;margin-bottom:32px;flex-wrap:wrap}
.pocket-btn{background:var(--gold);border:none;color:#fff;font-family:'Georgia',serif;
  font-size:13px;padding:9px 22px;border-radius:4px;cursor:pointer;letter-spacing:.3px}
.pocket-btn:hover{background:#a07820}
.pocket-btn.secondary{background:var(--paper2);color:var(--ink2);border:1px solid var(--border)}
.pocket-btn.secondary:hover{background:var(--border2)}
.pocket-btn.danger{background:var(--red)}
.pocket-btn.danger:hover{background:#6b1e08}
.pocket-form{background:var(--paper2);border:1px solid var(--border2);border-radius:4px;
  padding:18px 22px;max-width:480px;margin-bottom:24px;display:none}
.pocket-form.open{display:block}
.pocket-form-title{font-size:13px;letter-spacing:1px;color:var(--ink2);margin-bottom:12px}
.pocket-form input,.pocket-form textarea{width:100%;box-sizing:border-box;
  background:var(--paper);border:1px solid var(--border);color:var(--ink);
  font-family:'Georgia',serif;font-size:13px;padding:8px 11px;border-radius:3px;
  outline:none;margin-bottom:9px}
.pocket-form input:focus,.pocket-form textarea:focus{border-color:var(--gold)}
.pocket-form-row{display:flex;gap:9px}
.pocket-tx-list{max-width:640px}
.pocket-tx-item{display:flex;justify-content:space-between;align-items:flex-start;
  padding:9px 0;border-bottom:1px solid var(--border2);font-family:sans-serif;font-size:13px}
.pocket-tx-item:last-child{border-bottom:none}
.pocket-tx-note{color:var(--ink2);flex:1}
.pocket-tx-ts{font-size:10px;color:var(--ink3);margin-top:2px}
.pocket-tx-amount{font-weight:700;white-space:nowrap;margin-left:12px}
.pocket-tx-amount.income{color:var(--green)}
.pocket-tx-amount.expense{color:var(--red)}
.pocket-tx-amount.deferred{color:var(--gold)}
.pocket-section-title{font-size:11px;letter-spacing:3px;text-transform:uppercase;
  font-family:sans-serif;color:var(--ink3);margin-bottom:14px}
/* ── JOURNAL daily count ── */
.daily-done-badge{display:inline-flex;align-items:center;gap:6px;
  font-family:sans-serif;font-size:12px;color:var(--green);
  background:rgba(45,92,20,.08);border:1px solid rgba(45,92,20,.2);
  border-radius:10px;padding:2px 10px;margin-left:10px}
</style>
</head>
<body>

<nav>
  <div class="nav-logo">
    <div class="nav-game">Life RPG</div>
    <div class="nav-tagline">живая летопись судьбы</div>
  </div>
  <div class="nav-item active" data-s="journal" onclick="nav(this)">🗺️ Журнал</div>
  <div class="nav-item" data-s="missions" onclick="nav(this)">⚔️ Пути</div>
  <div class="nav-item" data-s="base" onclick="nav(this)">🗄️ База знаний</div>
  <div class="nav-item" data-s="pocket" onclick="nav(this)">💰 Карман</div>
  <div class="nav-bottom">
    <div id="nav-api-status" style="font-size:10px;font-family:sans-serif;color:var(--nav-dim);margin-bottom:6px"></div>
    <div style="cursor:pointer;font-size:11px;font-family:sans-serif;color:var(--nav-dim);
      padding:4px 0" onclick="openSettings()">⚙ Настройки Архивариуса</div>
    liferpg · v5
  </div>
</nav>

<header>
  <div class="hdr-title" id="hdr-title">Журнал</div>
  <div class="hdr-date" id="hdr-date"></div>
</header>

<main>

  <!-- JOURNAL -->
  <section id="s-journal" class="active">
    <div class="journal-wrap">
      <div class="journal-main" id="journal-main">
        <div class="empty">Дневник пуст. Напиши первую запись ↓</div>
      </div>
      <div class="journal-aside">
        <div class="aside-section">
          <div class="aside-label">Активные пути</div>
          <div id="aside-missions"></div>
        </div>
        <div class="aside-section">
          <div class="aside-label">Персонажи</div>
          <div id="aside-chars"></div>
        </div>
      </div>
    </div>
  </section>

  <!-- MISSIONS -->
  <section id="s-missions">
    <div class="missions-wrap">
      <div class="missions-topbar">
        <div>
          <div class="missions-eyebrow">Пути Героя</div>
          <div class="missions-heading">Пути</div>
        </div>
        <button class="btn-add" onclick="openDlg('mission-dlg')">+ Новый путь</button>
      </div>
      <div class="missions-sub">Пути, по которым идёт Герой. Задания рождаются из записей.</div>
      <div id="missions-list"></div>
    </div>
  </section>

  <!-- BASE OF KNOWLEDGE -->
  <section id="s-base">
    <div class="base-wrap" id="base-wrap">
      <div class="base-topbar">
        <div class="base-heading">🗄️ База знаний</div>
        <div class="base-topbar-right">
          <button class="btn-reanalyze" onclick="reanalyze()">⟳ Переосмыслить</button>
        </div>
      </div>
      <div id="base-content">
        <div class="empty">Загрузка базы знаний...</div>
      </div>
    </div>
  </section>

  <section id="s-pocket">
    <div style="margin-bottom:8px;display:flex;align-items:baseline;gap:14px">
      <div style="font-size:10px;letter-spacing:4px;text-transform:uppercase;color:var(--red);font-family:sans-serif">КАРМАН ГЕРОЯ</div>
    </div>
    <div style="font-size:22px;color:var(--ink);margin-bottom:4px">Карман</div>
    <div style="font-size:12px;color:var(--ink3);font-family:sans-serif;margin-bottom:28px">Учёт средств · баланс · резерв</div>

    <div class="pocket-cards" id="pocket-cards">
      <div class="pocket-card">
        <div class="pocket-card-label">Баланс</div>
        <div class="pocket-card-amount" id="pc-balance">—</div>
      </div>
      <div class="pocket-card deferred">
        <div class="pocket-card-label">Отложено</div>
        <div class="pocket-card-amount" id="pc-deferred">—</div>
      </div>
    </div>

    <div class="pocket-reserve-row">
      <span>Резерв:</span>
      <input type="number" id="pc-reserve-pct" min="0" max="99" value="20" style="width:52px">
      <span>% от каждого пополнения</span>
      <button class="pocket-btn secondary" style="padding:4px 14px;font-size:12px" onclick="savePocketCfg()">Сохранить</button>
    </div>

    <div class="pocket-actions">
      <button class="pocket-btn" onclick="togglePocketForm('income')">+ Пополнить</button>
      <button class="pocket-btn secondary" onclick="togglePocketForm('expense')">− Потратить</button>
      <button class="pocket-btn danger" onclick="togglePocketForm('deferred-spend')" style="font-size:12px">⚑ Из резерва</button>
      <button class="pocket-btn secondary" onclick="togglePocketForm('adjust')" style="font-size:12px">✎ Корректировка</button>
    </div>

    <div class="pocket-form" id="pf-income">
      <div class="pocket-form-title">ПОПОЛНЕНИЕ</div>
      <input type="number" id="pf-income-amount" placeholder="Сумма" min="0" step="0.01">
      <input type="text" id="pf-income-source" placeholder="Откуда пришли деньги">
      <div class="pocket-form-row">
        <button class="pocket-btn" onclick="submitPocketIncome()">Добавить</button>
        <button class="pocket-btn secondary" onclick="closePocketForms()">Отмена</button>
      </div>
    </div>

    <div class="pocket-form" id="pf-expense">
      <div class="pocket-form-title">РАСХОД</div>
      <input type="number" id="pf-expense-amount" placeholder="Сумма" min="0" step="0.01">
      <input type="text" id="pf-expense-note" placeholder="На что потрачено">
      <div class="pocket-form-row">
        <button class="pocket-btn secondary" onclick="submitPocketExpense(false)">Потратить</button>
        <button class="pocket-btn secondary" onclick="closePocketForms()">Отмена</button>
      </div>
    </div>

    <div class="pocket-form" id="pf-deferred-spend">
      <div class="pocket-form-title">РАСХОД ИЗ РЕЗЕРВА</div>
      <div style="font-size:12px;font-family:sans-serif;color:var(--ink3);margin-bottom:10px">Только на значимое для тебя.</div>
      <input type="number" id="pf-ds-amount" placeholder="Сумма" min="0" step="0.01">
      <input type="text" id="pf-ds-note" placeholder="Что это значит для тебя">
      <div class="pocket-form-row">
        <button class="pocket-btn danger" onclick="submitPocketExpense(true)">Потратить из резерва</button>
        <button class="pocket-btn secondary" onclick="closePocketForms()">Отмена</button>
      </div>
    </div>

    <div class="pocket-form" id="pf-adjust">
      <div class="pocket-form-title">КОРРЕКТИРОВКА</div>
      <div style="font-size:12px;font-family:sans-serif;color:var(--ink3);margin-bottom:10px">Положительное число увеличивает, отрицательное — уменьшает.</div>
      <select id="pf-adjust-target" style="width:100%;margin-bottom:8px;background:var(--paper);border:1px solid var(--border);color:var(--ink);padding:7px;border-radius:2px">
        <option value="balance">Баланс</option>
        <option value="deferred">Резерв (отложено)</option>
      </select>
      <input type="number" id="pf-adjust-amount" placeholder="Сумма (может быть отрицательной)" step="0.01">
      <input type="text" id="pf-adjust-note" placeholder="Причина корректировки">
      <div class="pocket-form-row">
        <button class="pocket-btn secondary" onclick="submitPocketAdjust()">Применить</button>
        <button class="pocket-btn secondary" onclick="closePocketForms()">Отмена</button>
      </div>
    </div>

    <div class="pocket-section-title">ИСТОРИЯ</div>
    <div class="pocket-tx-list" id="pocket-tx-list"><div class="empty">Загрузка...</div></div>
  </section>

</main>

<div id="input-bar">
  <textarea id="txt" placeholder="Что произошло? Говори свободно... (Cmd+Enter)"></textarea>
  <button id="send-btn" onclick="sendEntry()">Записать →</button>
</div>

<!-- Settings Modal -->
<div id="settings-modal">
  <div id="settings-box">
    <div class="settings-title">⚙ Настройки · Архивариус</div>
    <div id="settings-key-status" class="settings-status missing" style="margin-bottom:16px">Проверка...</div>

    <div class="settings-label">GigaChat (Сбер)</div>
    <input class="settings-input" id="settings-gc-key" type="password" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx">
    <div class="settings-hint">Авторизационный ключ из <b>developers.sber.ru</b> → GigaChat API</div>
    <div style="display:flex;gap:10px;margin-bottom:18px">
      <button class="pocket-btn" onclick="saveGigaChat()">Сохранить GigaChat</button>
    </div>

    <div class="settings-label">Anthropic Claude (опционально)</div>
    <input class="settings-input" id="settings-api-key" type="password" placeholder="sk-ant-...">
    <div class="settings-hint" style="margin-bottom:14px">console.anthropic.com → API Keys</div>
    <div style="display:flex;gap:10px">
      <button class="pocket-btn" onclick="saveApiKey()">Сохранить Anthropic</button>
      <button class="pocket-btn secondary" onclick="closeSettings()">Закрыть</button>
    </div>
  </div>
</div>

<!-- Entity Modal -->
<div id="ent-modal">
  <div id="ent-box">
    <button id="ent-close" onclick="closeEnt()">✕</button>
    <div id="ent-content"></div>
  </div>
</div>


<!-- Dialogs -->
<div class="dlg" id="mission-dlg">
  <div class="dlg-box">
    <div class="dlg-title">Новый путь</div>
    <input class="dlg-input" id="m-title" placeholder="Куда ведёт этот путь?">
    <textarea class="dlg-textarea" id="m-desc" placeholder="Зачем ты идёшь этим путём?"></textarea>
    <div class="dlg-btns">
      <button class="btn-cancel" onclick="closeDlg('mission-dlg')">Отмена</button>
      <button class="btn-primary" onclick="saveMission()">Создать</button>
    </div>
  </div>
</div>

<div class="dlg" id="task-dlg">
  <div class="dlg-box">
    <div class="dlg-title">Добавить задание</div>
    <input class="dlg-input" id="t-title" placeholder="Название задания">
    <div style="display:flex;gap:16px;margin-bottom:12px;font-family:sans-serif;font-size:13px;color:var(--ink2)">
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="radio" name="t-type" value="once" checked onchange="toggleRepeatOpts(this)"> Разовое
      </label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="radio" name="t-type" value="repeat" onchange="toggleRepeatOpts(this)"> Повторяемое
      </label>
    </div>
    <div id="t-repeat-opts" style="display:none;gap:8px;margin-bottom:10px;flex-wrap:wrap">
      <div style="display:flex;align-items:center;gap:6px">
        <input class="dlg-input" id="t-iters" type="number" min="1" value="1"
          style="width:90px;margin-bottom:0" placeholder="Раз">
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">раз за цикл</span>
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <input class="dlg-input" id="t-hours" type="number" min="1" value="24"
          style="width:90px;margin-bottom:0" placeholder="Часов">
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">ч до сброса</span>
      </div>
    </div>
    <input type="hidden" id="t-mid">
    <div class="dlg-btns">
      <button class="btn-cancel" onclick="closeDlg('task-dlg')">Отмена</button>
      <button class="btn-primary" onclick="saveTask()">Добавить</button>
    </div>
  </div>
</div>


<script>
// ── State ────────────────────────────────────────────────────────────────────
let allEntities = [];
let _openMissions = new Set();   // expanded mission blocks
let _closedMissions = new Set(); // manually collapsed by user

const ICONS = {person:'👤',place:'📍',project:'📁',concept:'💡',event:'📅'};
const TYPE_COLORS = {
  person:'var(--blue)', place:'#6b4a22', project:'var(--green)',
  concept:'#7b4fa0', event:'var(--gold)', quest:'var(--red)'
};

// ── Live astro (wttr.in) ─────────────────────────────────────────────────────
let _liveAstro = null;
const PHASE_EN_RU = {
  'New Moon':        {ru:'Новолуние',          emoji:'🌑'},
  'Waxing Crescent': {ru:'Молодой месяц',      emoji:'🌒'},
  'First Quarter':   {ru:'Первая четверть',    emoji:'🌓'},
  'Waxing Gibbous':  {ru:'Прибывающая луна',   emoji:'🌔'},
  'Full Moon':       {ru:'Полнолуние',          emoji:'🌕'},
  'Waning Gibbous':  {ru:'Убывающая луна',     emoji:'🌖'},
  'Last Quarter':    {ru:'Последняя четверть', emoji:'🌗'},
  'Waning Crescent': {ru:'Старый месяц',       emoji:'🌘'},
};

async function fetchAstroData() {
  try {
    const r = await fetch('https://wttr.in/?format=j1',{headers:{'Accept':'application/json'}});
    const d = await r.json();
    const a = d.weather[0].astronomy[0];
    _liveAstro = {phase_en:a.moon_phase, illumination:parseInt(a.moon_illumination),
                  moonrise:a.moonrise, moonset:a.moonset,
                  sunrise:a.sunrise, sunset:a.sunset, source:'wttr.in'};
    tickClock();
    if(document.querySelector('#s-journal.active')) loadJournal();
    return;
  } catch(e) {}
  try {
    const r=await fetch('/moonphase'); const d=await r.json();
    if(d.phase_en){ _liveAstro=d; tickClock(); }
  } catch(e) {}
}
fetchAstroData(); setInterval(fetchAstroData, 6*60*60*1000);

// ── КОЛЕСО МИРОВ ─────────────────────────────────────────────────────────────
const WoW = {
  EPOCH_START:2026, EPOCH_START_MONTH:5, EPOCH_START_DAY:4, EPOCH_NAME:'Эпоха Пепла',
  SEASONS:[
    {name:'Пробуждение',element:'Воздух',emoji:'🌬️',moons:['Луна Первого Ветра','Луна Семян','Луна Голоса']},
    {name:'Зной',element:'Огонь',emoji:'🔥',moons:['Луна Меча','Луна Пепла','Луна Жажды']},
    {name:'Жатва',element:'Земля',emoji:'🌾',moons:['Луна Весов','Луна Золота','Луна Договора']},
    {name:'Угасание',element:'Эфир',emoji:'🌫️',moons:['Луна Завесы','Луна Теней','Луна Памяти']},
    {name:'Мороз',element:'Вода',emoji:'❄️',moons:['Луна Прилива','Луна Молчания','Луна Возврата']},
  ],
  PHASES:[
    {name:'Новолуние',emoji:'🌑',lo:0.000,hi:0.033,effect:'Мёртвая вода. Тайны открываются.'},
    {name:'Молодой месяц',emoji:'🌒',lo:0.033,hi:0.240,effect:'Малый прилив. Торговые суда выходят.'},
    {name:'Первая четверть',emoji:'🌓',lo:0.240,hi:0.285,effect:'Равновесие сил. День решений.'},
    {name:'Прибывающая луна',emoji:'🌔',lo:0.285,hi:0.465,effect:'Прилив нарастает. Хорошее время.'},
    {name:'Полнолуние',emoji:'🌕',lo:0.465,hi:0.535,effect:'Большой прилив. Ритуалы. Чудовища у берегов.'},
    {name:'Убывающая луна',emoji:'🌖',lo:0.535,hi:0.715,effect:'Отлив начинается. Артефакты на дне.'},
    {name:'Последняя четверть',emoji:'🌗',lo:0.715,hi:0.760,effect:'День переосмысления.'},
    {name:'Старый месяц',emoji:'🌘',lo:0.760,hi:1.000,effect:'Духи говорят в тишине.'},
  ],
  PATRONS:[
    {name:'Феникс',emoji:'🦅',months:[3,4]},
    {name:'Страж',emoji:'⚔️',months:[5,6]},
    {name:'Дракон',emoji:'🐉',months:[7,8]},
    {name:'Весы',emoji:'⚖️',months:[9,10]},
    {name:'Левиафан',emoji:'🐋',months:[11]},
    {name:'Пустота',emoji:'🌌',months:[12,1]},
    {name:'Сеть',emoji:'🕸️',months:[2]},
  ],
  _jd(y,m,d){
    let Y=y,M=m; if(M<=2){Y--;M+=12;}
    const A=Math.floor(Y/100),B=2-A+Math.floor(A/4);
    return Math.floor(365.25*(Y+4716))+Math.floor(30.6001*(M+1))+d+B-1524.5;
  },
  _moonPhase(y,m,d){
    const JD=this._jd(y,m,d+0.5),REF=2451550.1,SYN=29.530588853;
    return (((JD-REF)/SYN%1)+1)%1;
  },
  _illumination(p){return Math.round((1-Math.cos(2*Math.PI*p))/2*100);},
  _season(y,doy){
    const lp=((y%4===0&&y%100!==0)||(y%400===0))?1:0;
    const sp=79+lp,su=172+lp,au=266+lp,wi=355+lp;
    if(doy>=sp&&doy<su)return{real:'spring',prog:(doy-sp)/(su-sp)};
    if(doy>=su&&doy<au)return{real:'summer',prog:(doy-su)/(au-su)};
    if(doy>=au&&doy<wi)return{real:'autumn',prog:(doy-au)/(wi-au)};
    return{real:'winter',prog:doy>=wi?(doy-wi)/(sp+365-wi):(doy+(365+lp-wi))/(sp+365-wi)};
  },
  _patron(m,d){
    const z=(m==3&&d>=21)||(m==4&&d<=19)?3:(m==4&&d>=20)||(m==5&&d<=20)?4:
            (m==5&&d>=21)||(m==6&&d<=20)?5:(m==6&&d>=21)||(m==7&&d<=22)?6:
            (m==7&&d>=23)||(m==8&&d<=22)?7:(m==8&&d>=23)||(m==9&&d<=22)?8:
            (m==9&&d>=23)||(m==10&&d<=22)?9:(m==10&&d>=23)||(m==11&&d<=21)?10:
            (m==11&&d>=22)||(m==12&&d<=21)?11:(m==12&&d>=22)||(m==1&&d<=19)?12:2;
    return this.PATRONS.find(p=>p.months.includes(z))||this.PATRONS[0];
  },
  convert(dateStr){
    const parts=dateStr.split(' ');
    const [y,m,d]=parts[0].split('-').map(Number);
    const phase=this._moonPhase(y,m,d);
    const illum=this._illumination(phase);
    const dSinceNew=phase*29.530588853;
    const dayInMoon=Math.floor(dSinceNew)+1;
    const phaseObj=this.PHASES.find(p=>phase>=p.lo&&phase<p.hi)||this.PHASES[7];
    const jan1=new Date(y,0,1);
    const doy=Math.round((new Date(y,m-1,d)-jan1)/86400000)+1;
    // Сезоны по месяцам: Пробуждение=3-4, Зной=5-6, Жатва=7-8, Угасание=9-10, Мороз=11-2
    const sIdxByMonth=[4,4,0,0,1,1,2,2,3,3,4,4]; // jan=0..dec=11
    const sIdx=sIdxByMonth[m-1];
    const season=this.SEASONS[sIdx];
    // Луна внутри сезона по дню месяца
    const mIdx=d<=10?0:d<=20?1:2;
    const moon=season.moons[mIdx];
    const patron=this._patron(m,d);
    const epochStart=new Date(this.EPOCH_START,this.EPOCH_START_MONTH-1,this.EPOCH_START_DAY);
    const nowDate=new Date(y,m-1,d);
    const epochYear=Math.floor((nowDate-epochStart)/(365.25*24*3600*1000))+1;
    const todayStr=(()=>{const n=new Date();return `${n.getFullYear()}-${String(n.getMonth()+1).padStart(2,'0')}-${String(n.getDate()).padStart(2,'0')}`;})();
    const isToday=parts[0]===todayStr;
    let phaseName,phaseEmoji,phaseEffect,finalIllum;
    if(isToday&&_liveAstro&&_liveAstro.phase_en){
      const p=PHASE_EN_RU[_liveAstro.phase_en]||{ru:_liveAstro.phase_en,emoji:'🌙'};
      phaseName=p.ru; phaseEmoji=p.emoji;
      phaseEffect=WoW.PHASES.find(x=>x.name===p.ru)?.effect||'';
      finalIllum=_liveAstro.illumination;
    } else {
      phaseName=phaseObj.name; phaseEmoji=phaseObj.emoji;
      phaseEffect=phaseObj.effect; finalIllum=illum;
    }
    const liveInfo=(isToday&&_liveAstro)?_liveAstro:null;
    const sunStr=liveInfo?`☀️ ${liveInfo.sunrise} → ${liveInfo.sunset}`:'';
    return {
      epochYear,epochName:this.EPOCH_NAME,
      season:season.name,element:season.element,seasonEmoji:season.emoji,
      moon,moonIdx:mIdx,phase:phaseName,phaseEmoji,phaseEffect,
      phaseVal:phase,illumination:finalIllum,dayInMoon,
      patron:patron.name,patronEmoji:patron.emoji,
      isLive:isToday&&!!_liveAstro,
      sunrise:liveInfo?.sunrise,sunset:liveInfo?.sunset,
      moonrise:liveInfo?.moonrise,moonset:liveInfo?.moonset,
      heading:moon.toUpperCase()+` · ДЕНЬ ${dayInMoon}`,
      sub:`${season.name} ${season.emoji} · ${phaseEmoji} ${phaseName} · ${finalIllum}%`+
          ` · ${patron.emoji} ${patron.name} · ${epochYear}-й год ${this.EPOCH_NAME}`+
          (sunStr?` · ${sunStr}`:''),
      short:`${moon} · ${phaseEmoji} ${finalIllum}% · День ${dayInMoon}`,
    };
  },
  now(){const n=new Date();return this.convert(`${n.getFullYear()}-${String(n.getMonth()+1).padStart(2,'0')}-${String(n.getDate()).padStart(2,'0')}`);}
};

// ── Clock ────────────────────────────────────────────────────────────────────
function tickClock(){
  const cal=WoW.now();
  const t=new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
  document.getElementById('hdr-date').innerHTML=
    `<span style="color:var(--red);font-weight:700">${cal.short}</span>`+
    `&ensp;<span style="color:var(--ink3);font-size:11px">${t}</span>`;
}
tickClock(); setInterval(tickClock,30000);

// ── Nav ──────────────────────────────────────────────────────────────────────
const TITLES={journal:'Журнал',missions:'Пути',base:'База знаний',pocket:'Карман'};
function nav(el){
  document.querySelectorAll('.nav-item').forEach(i=>i.classList.remove('active'));
  el.classList.add('active');
  const s=el.dataset.s;
  document.querySelectorAll('section').forEach(i=>i.classList.remove('active'));
  document.getElementById('s-'+s).classList.add('active');
  document.getElementById('hdr-title').textContent=TITLES[s]||s;
  if(s==='journal'){loadJournal();loadAsides();}
  if(s==='missions') loadMissions();
  if(s==='base') loadBase();
  if(s==='pocket') loadPocket();
}

// ── Linkify ──────────────────────────────────────────────────────────────────
function linkify(text){
  if(!allEntities.length) return text;
  const sorted=[...allEntities].sort((a,b)=>b.name.length-a.name.length);
  let out=text;
  for(const ent of sorted){
    const esc=ent.name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    const re=new RegExp(`(?<![a-zA-Zа-яёА-ЯЁ])(${esc})(?![a-zA-Zа-яёА-ЯЁ])`,'g');
    const cls='ent-link type-'+(ent.type||'concept');
    out=out.replace(re,`<span class="${cls}" onclick="openEnt('${ent.name.replace(/'/g,"\\'")}')">$1</span>`);
  }
  return out;
}

// ── Journal ──────────────────────────────────────────────────────────────────
async function loadJournal(){
  const [dr,er,ir,doneR]=await Promise.all([fetch('/diary'),fetch('/entities'),fetch('/inbox'),fetch('/tasks/completed-today')]);
  const diary=await dr.json(); allEntities=await er.json();
  const inboxRaw=await ir.json();
  const doneToday=(await doneR.json()).count||0;
  // Only entries actually in the inbox queue are truly "pending"
  const pendingIds=new Set(inboxRaw.filter(i=>!i.type||i.type==='entry').map(i=>i.id));
  const el=document.getElementById('journal-main');
  if(!diary.length){
    el.innerHTML='<div class="empty">Дневник пуст. Напиши первую запись внизу ↓</div>';
    return;
  }
  const byDate={};
  for(const e of diary){
    const d=e.ts.split(' ')[0];
    if(!byDate[d])byDate[d]=[];
    byDate[d].push(e);
  }
  el.innerHTML=Object.entries(byDate).map(([date,entries])=>{
    const cal=WoW.convert(date);
    const items=entries.map(e=>{
      const timeStr=e.ts.includes(' ')?e.ts.split(' ')[1]:'';
      const isPending = pendingIds.has(e.id);
      const archivistHtml=e.archivist_note?
        `<div class="entry-archivist">◆ ${e.archivist_note}</div>`:'';
      const pendingHtml=isPending?
        `<div style="font-size:11px;color:var(--border);font-family:sans-serif;font-style:italic;margin-top:8px">
          ⏳ Архивариус обрабатывает запись...
        </div>`:'';
      return `<div class="entry">
        <div class="entry-text">${linkify(e.narrative)}</div>
        ${!isPending&&e.raw&&e.raw!==e.narrative?`<div class="entry-raw">«${e.raw}»</div>`:''}
        ${archivistHtml}
        ${pendingHtml}
        ${timeStr?`<div class="entry-time">${timeStr} · ${cal.phaseEmoji} ${cal.phase}</div>`:''}
      </div>`;
    }).join('');
    const isToday=date===new Date().toISOString().slice(0,10);
    const doneBadge=isToday&&doneToday>0?`<span class="daily-done-badge">✓ ${doneToday} заданий сегодня</span>`:'';
    return `<div class="day-block">
      <div class="day-heading">${cal.heading}${doneBadge}</div>
      <div class="day-sub">${cal.sub}</div>
      ${items}
    </div>`;
  }).join('');
}

async function loadAsides(){
  const [mr,er]=await Promise.all([fetch('/missions'),fetch('/entities?type=person')]);
  const missions=await mr.json(), persons=await er.json();
  const active=missions.filter(m=>m.status==='active');
  document.getElementById('aside-missions').innerHTML=active.length
    ?active.map(m=>`<div class="aside-mission" onclick="nav(document.querySelector('[data-s=missions]'))">
      <div class="aside-mission-t">${m.title}</div></div>`).join('')
    :'<div style="font-size:12px;color:var(--ink3);font-family:sans-serif">нет</div>';
  document.getElementById('aside-chars').innerHTML=persons.slice(0,5).map(p=>`
    <div class="aside-entity" onclick="openEnt('${p.name.replace(/'/g,"\\'")}')">
      <div class="aside-entity-ic">${ICONS[p.type]||'◆'}</div>
      <div>
        <div class="aside-entity-name">${p.name}</div>
        <div class="aside-entity-sub">${(p.summary||'').slice(0,36)}…</div>
      </div>
    </div>`).join('')||'<div style="font-size:12px;color:var(--ink3);font-family:sans-serif">нет</div>';
}

// ── Ingest ───────────────────────────────────────────────────────────────────
async function sendEntry(){
  const txt=document.getElementById('txt'),
        btn=document.getElementById('send-btn');
  if(!txt.value.trim()) return;
  btn.disabled=true; btn.textContent='⏳...';
  try {
    const r=await fetch('/ingest',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:txt.value})});
    const d=await r.json();
    txt.value='';
    const active=document.querySelector('section.active');
    if(active?.id==='s-journal'){loadJournal();loadAsides();}
    if(active?.id==='s-missions') loadMissions();
  } catch(e){ console.error('ingest error',e); }
  finally{ btn.disabled=false; btn.textContent='Записать →'; }
}
document.getElementById('txt').addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='Enter') sendEntry();
});

// ── Missions ─────────────────────────────────────────────────────────────────
function toggleMission(mid){
  const chain=document.getElementById('qchain-'+mid);
  const chev=document.getElementById('chev-'+mid);
  if(!chain) return;
  const isOpen=chain.classList.contains('open');
  if(isOpen){
    chain.classList.remove('open'); chev.classList.remove('open');
    _openMissions.delete(mid); _closedMissions.add(mid);
  } else {
    chain.classList.add('open'); chev.classList.add('open');
    _openMissions.add(mid); _closedMissions.delete(mid);
  }
}

async function loadMissions(){
  const r=await fetch('/missions'); const ms=await r.json();
  const el=document.getElementById('missions-list');
  if(!ms.length){
    el.innerHTML='<div class="empty">Нет путей. Добавь первый путь ↗</div>';
    return;
  }
  ms.forEach(m=>{ if(!_closedMissions.has(m.id)) _openMissions.add(m.id); });
  el.innerHTML=ms.map(m=>{
    const onceDone=m.tasks.filter(t=>t.task_type!=='repeat'&&t.status==='done').length;
    const onceTotal=m.tasks.filter(t=>t.task_type!=='repeat').length;
    const repeatTotal=m.tasks.filter(t=>t.task_type==='repeat').length;
    const totalCount=m.tasks.length;
    const wasOpen=_openMissions.has(m.id);

    let archivistHtml='';
    if(m.status!=='done'){
      if(totalCount===0){
        archivistHtml=`<div class="archivist-warning">⚠ Путь во мгле. Архивариус не видит ни одного задания. Поведай о своих намерениях — и путь прояснится.</div>`;
      } else if(onceDone===onceTotal&&onceTotal>0&&repeatTotal===0){
        archivistHtml=`<div class="archivist-wisdom">✦ Все разовые задания пройдены. Архивариус полагает: цель близко. Скажи слово — и мир запишет победу.</div>`;
      }
    }

    const tasks=m.tasks.map(t=>{
      const isRepeat=t.task_type==='repeat';
      const cycled=isRepeat&&t.current_iters>=t.required_iters;
      if(isRepeat){
        const hrs=t.reset_hours===24?'24ч (ежедн.)':t.reset_hours===1?'1ч':t.reset_hours+'ч';
        return `
        <div class="quest-item repeat-task">
          <div class="quest-info" style="flex:1">
            <div style="display:flex;align-items:center;gap:7px">
              <span class="repeat-badge">🔄 повтор</span>
              <div class="quest-title" id="qtitle-${t.id}">${t.title}</div>
              <button class="btn-edit-inline" onclick="editTask('${t.id}');event.stopPropagation()" title="Редактировать">✎</button>
            </div>
            <div class="repeat-progress">
              <button class="iter-btn" onclick="tickTask('${t.id}','${m.id}')" ${cycled?'disabled':''}>+1</button>
              <span class="iter-count ${cycled?'done':''}">${t.current_iters}/${t.required_iters}</span>
              <span class="streak-display">🔥&thinsp;${t.streak}</span>
              <span class="streak-best">рекорд: ${t.best_streak}</span>
              <span class="reset-hint" data-reset-ts="${t.last_reset_ts||''}" data-reset-hours="${t.reset_hours||24}">· ${fmtCountdown(t.last_reset_ts,t.reset_hours)}</span>
            </div>
          </div>
          <button class="quest-del" onclick="deleteTask('${t.id}','${m.id}')" title="Удалить">×</button>
        </div>`;
      }
      const isDone=t.status==='done';
      return `
      <div class="quest-item">
        <div class="quest-cb ${isDone?'done':''}" onclick="doneTask('${t.id}','${m.id}')">${isDone?'✓':''}</div>
        <div class="quest-info">
          <div class="quest-title ${isDone?'done':''}" id="qtitle-${t.id}">${t.title}</div>
          <div class="quest-ts">${t.ts||''}</div>
        </div>
        <button class="btn-edit-inline" onclick="editTask('${t.id}');event.stopPropagation()" title="Редактировать">✎</button>
        <button class="quest-del" onclick="deleteTask('${t.id}','${m.id}')" title="Удалить">×</button>
      </div>`;
    }).join('');

    const badgeText=onceTotal?`${onceDone}/${onceTotal} разовых`:'';
    const repeatBadge=repeatTotal?`${repeatTotal} повтор.`:'';
    const badge=[badgeText,repeatBadge].filter(Boolean).join(' · ');

    return `
    <div class="mission-block ${m.status==='done'?'done':''}">
      <div class="mission-block-hdr" onclick="toggleMission('${m.id}')">
        <div class="mission-star">${m.status==='done'?'✓':'✦'}</div>
        <div class="mission-block-info">
          <div class="mission-block-title" id="mtitle-${m.id}">${m.title}</div>
          ${badge?`<div class="mission-progress-badge">${badge}</div>`:''}
        </div>
        <button class="btn-edit-inline" onclick="editMission('${m.id}');event.stopPropagation()" title="Редактировать">✎</button>
        <div class="mission-block-chevron ${wasOpen?'open':''}" id="chev-${m.id}">▾</div>
      </div>
      ${m.lore?`<div class="mission-lore">${m.lore}</div>`:''}
      <div class="quest-chain ${wasOpen?'open':''}" id="qchain-${m.id}">
        ${archivistHtml}
        ${tasks}
      </div>
      <div class="mission-actions">
        ${m.status!=='done'?`<button class="btn-quest-add" onclick="openTaskDlg('${m.id}')">+ добавить задание</button>`:''}
        ${m.status!=='done'?`<button class="btn-sm btn-ok" onclick="doneMission('${m.id}')">✓ Путь пройден</button>`:''}
        <button class="btn-sm btn-danger" onclick="deleteMission('${m.id}')">× удалить</button>
      </div>
    </div>`;
  }).join('');
  _openMissions.forEach(mid=>{
    const chain=document.getElementById('qchain-'+mid);
    const chev=document.getElementById('chev-'+mid);
    if(chain) chain.classList.add('open');
    if(chev) chev.classList.add('open');
  });
}

async function saveMission(){
  const t=document.getElementById('m-title').value.trim();
  const d=document.getElementById('m-desc').value.trim();
  if(!t) return;
  const res=await fetch('/missions',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:t,description:d})});
  const data=await res.json();
  closeDlg('mission-dlg');
  document.getElementById('m-title').value='';
  document.getElementById('m-desc').value='';
  if(data.id) _openMissions.add(data.id); // auto-expand new mission
  loadMissions();
}
function openTaskDlg(mid){
  document.getElementById('t-mid').value=mid;
  openDlg('task-dlg');
  setTimeout(()=>document.getElementById('t-title').focus(),50);
}
async function saveTask(){
  const t=document.getElementById('t-title').value.trim();
  const mid=document.getElementById('t-mid').value;
  if(!t) return;
  const typeEl=document.querySelector('input[name="t-type"]:checked');
  const taskType=typeEl?typeEl.value:'once';
  const iters=parseInt(document.getElementById('t-iters')?.value)||1;
  const hours=parseInt(document.getElementById('t-hours')?.value)||24;
  await fetch('/tasks',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mission_id:mid,title:t,task_type:taskType,required_iters:iters,reset_hours:hours})});
  closeDlg('task-dlg');
  document.getElementById('t-title').value='';
  const onceRadio=document.querySelector('input[name="t-type"][value="once"]');
  if(onceRadio) onceRadio.checked=true;
  const repeatOpts=document.getElementById('t-repeat-opts');
  if(repeatOpts) repeatOpts.style.display='none';
  _openMissions.add(mid);
  loadMissions();
}
async function tickTask(tid,mid){
  await fetch(`/tasks/${tid}/tick`,{method:'POST'});
  _openMissions.add(mid); loadMissions();
}
function toggleRepeatOpts(el){
  const opts=document.getElementById('t-repeat-opts');
  if(opts) opts.style.display=el.value==='repeat'?'flex':'none';
}
async function doneMission(id){
  await fetch(`/missions/${id}/complete`,{method:'POST'}); loadMissions();
}
async function deleteMission(id){
  if(!confirm('Удалить этот путь и все его задания?')) return;
  await fetch(`/missions/${id}/delete`,{method:'POST'}); loadMissions();
}
async function doneTask(tid,mid){
  await fetch(`/tasks/${tid}/complete`,{method:'POST'});
  _openMissions.add(mid); loadMissions();
}
async function deleteTask(tid,mid){
  await fetch(`/tasks/${tid}/delete`,{method:'POST'});
  _openMissions.add(mid); loadMissions();
}

// ── Inline edit ───────────────────────────────────────────────────────────────
function editMission(mid){
  const el=document.getElementById('mtitle-'+mid);
  if(!el||el.querySelector('input')) return;
  const cur=el.textContent;
  el.innerHTML=`<input class="inline-edit-input" id="medit-${mid}"
    value="${cur.replace(/"/g,'&quot;')}"
    onblur="saveMissionEdit('${mid}')"
    onkeydown="if(event.key==='Enter'){event.preventDefault();saveMissionEdit('${mid}');}
               if(event.key==='Escape'){event.preventDefault();loadMissions();}">`;
  const inp=document.getElementById('medit-'+mid);
  inp.focus(); inp.select();
}
async function saveMissionEdit(mid){
  const inp=document.getElementById('medit-'+mid);
  if(!inp) return;
  const val=inp.value.trim();
  if(!val){loadMissions();return;}
  // Get current description
  await fetch(`/missions/${mid}/update`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:val,description:''})});
  loadMissions();
}

function editTask(tid){
  const el=document.getElementById('qtitle-'+tid);
  if(!el||el.querySelector('input')) return;
  const cur=el.textContent;
  el.innerHTML=`<input class="inline-edit-input" id="tedit-${tid}"
    value="${cur.replace(/"/g,'&quot;')}"
    onblur="saveTaskEdit('${tid}')"
    onkeydown="if(event.key==='Enter'){event.preventDefault();saveTaskEdit('${tid}');}
               if(event.key==='Escape'){event.preventDefault();loadMissions();}">`;
  const inp=document.getElementById('tedit-'+tid);
  inp.focus(); inp.select();
}
async function saveTaskEdit(tid){
  const inp=document.getElementById('tedit-'+tid);
  if(!inp) return;
  const val=inp.value.trim();
  if(!val){loadMissions();return;}
  await fetch(`/tasks/${tid}/update`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:val})});
  loadMissions();
}

// ── Base of Knowledge ─────────────────────────────────────────────────────────
async function loadBase(){
  const [er,gr]=await Promise.all([fetch('/entities'),fetch('/graph')]);
  const entities=await er.json(); allEntities=entities;
  const graph=await gr.json();

  // Build link map: entity name → outgoing relations
  const linkMap={};
  for(const r of graph){
    if(!linkMap[r.from])linkMap[r.from]=[];
    linkMap[r.from].push(r);
  }

  // Group by type
  const order=['person','place','project','concept','event'];
  const labels={person:'👤 Люди',place:'📍 Места',project:'📁 Проекты',
                concept:'💡 Концепции',event:'📅 События'};
  const groups={};
  for(const e of entities){
    const t=e.type||'concept';
    if(!groups[t])groups[t]=[];
    groups[t].push(e);
  }

  const el=document.getElementById('base-content');
  const typeOrder=[...order,...Object.keys(groups).filter(k=>!order.includes(k))];
  const html=typeOrder.filter(t=>groups[t]?.length).map(type=>{
    const ents=groups[type];
    const clr=TYPE_COLORS[type]||'var(--ink3)';
    const cards=ents.map(e=>{
      const rels=(linkMap[e.name]||[]).slice(0,4);
      const relsHtml=rels.map(r=>`<span class="bcard-rel" onclick="event.stopPropagation();openEnt('${r.to.replace(/'/g,"\\'")}')">→ ${r.to}</span>`).join('');
      const tagsArr=Array.isArray(e.tags)?e.tags:typeof e.tags==='string'&&e.tags?e.tags.split(',').filter(Boolean):[];
      const tagsHtml=tagsArr.map(t=>`<span class="bcard-tag">${t}</span>`).join('');
      return `<div class="bcard" onclick="openEnt('${e.name.replace(/'/g,"\\'")}')">
        <div class="bcard-stripe" style="background:${clr}"></div>
        <div class="bcard-name">${e.name}</div>
        <div class="bcard-type" style="color:${clr}">${labels[type]||type}</div>
        <div class="bcard-summary">${e.summary||''}</div>
        ${relsHtml?`<div class="bcard-rels">${relsHtml}</div>`:''}
        ${tagsHtml?`<div class="bcard-tags">${tagsHtml}</div>`:''}
        <div class="bcard-footer">нажми → полная карточка</div>
      </div>`;
    }).join('');
    return `<div class="base-section">
      <div class="base-sec-hdr">
        <div class="base-sec-title" style="color:${clr}">${labels[type]||type}</div>
        <div class="base-sec-count">${ents.length} записей</div>
      </div>
      <div class="base-grid">${cards}</div>
    </div>`;
  }).join('');

  el.innerHTML=html||'<div class="empty">База пуста. Записи в журнале породят сущности здесь.</div>';
}

// ── Переосмыслить ────────────────────────────────────────────────────────────
async function reanalyze(){
  const btn=document.querySelector('.btn-reanalyze');
  btn.disabled=true; btn.textContent='⟳ Запрос отправлен...';
  try{
    const d=await (await fetch('/reanalyze',{method:'POST'})).json();
    if(d.status==='no_api_key'){
      btn.textContent='⟳ Переосмыслить'; btn.disabled=false;
      openSettings(); return;
    }
    btn.textContent='⟳ Архивариус анализирует...';
    _pollReanalyze();
  }catch(e){
    btn.textContent='⟳ Переосмыслить'; btn.disabled=false;
  }
}
function _pollReanalyze(){
  const btn=document.querySelector('.btn-reanalyze');
  let dots=0;
  const labels=['⟳ Архивариус анализирует','⟳ Архивариус анализирует·','⟳ Архивариус анализирует··','⟳ Архивариус анализирует···'];
  const iv=setInterval(async()=>{
    dots=(dots+1)%4; btn.textContent=labels[dots];
    try{
      const s=await fetch('/reanalyze/status');
      const sd=await s.json();
      if(!sd.running){
        clearInterval(iv);
        btn.textContent='⟳ Переосмыслить'; btn.disabled=false;
        loadMissions(); loadAsides();
        if(document.querySelector('#s-base.active')) loadBase();
      }
    }catch(e){ clearInterval(iv); btn.textContent='⟳ Переосмыслить'; btn.disabled=false; }
  },5000);
  setTimeout(()=>{ clearInterval(iv); btn.textContent='⟳ Переосмыслить'; btn.disabled=false; },300000);
}

// ── Entity Modal ─────────────────────────────────────────────────────────────
async function openEnt(name){
  const r=await fetch('/entity/'+encodeURIComponent(name));
  if(!r.ok) return;
  const e=await r.json();
  const clr=TYPE_COLORS[e.type]||'var(--ink3)';
  const out=e.links_out.map(r=>
    `<div class="rel-row">
      <span class="rel-badge">${r.label}</span>
      <span class="rel-name" onclick="closeEnt();openEnt('${r.to.replace(/'/g,"\\'")}')">→ ${r.to}</span>
    </div>`).join('')||'<div style="color:var(--ink3);font-size:12px;font-family:sans-serif">нет</div>';
  const inp=e.links_in.map(r=>
    `<div class="rel-row">
      <span class="rel-name" onclick="closeEnt();openEnt('${r.from.replace(/'/g,"\\'")}')">← ${r.from}</span>
      <span class="rel-badge">${r.label}</span>
    </div>`).join('')||'<div style="color:var(--ink3);font-size:12px;font-family:sans-serif">нет</div>';
  const ments=e.mentions.map(m=>
    `<div class="ment-ts">${m.ts}</div>
    <div class="ment-text">${m.narrative}</div>
    ${m.archivist_note?`<div class="ment-archivist">◆ ${m.archivist_note}</div>`:''}`).join('');
  const entTagsArr=Array.isArray(e.tags)?e.tags:typeof e.tags==='string'&&e.tags?e.tags.split(',').filter(Boolean):[];
  const tagsHtml=entTagsArr.length
    ?`<div class="ent-tags">${entTagsArr.map(t=>`<span class="ent-tag">${t}</span>`).join('')}</div>`:'';

  document.getElementById('ent-content').innerHTML=`
    <div class="ent-name" style="color:${clr}">${ICONS[e.type]||'◆'} ${e.name}</div>
    <div class="ent-type" style="color:${clr}">${e.type}</div>
    ${tagsHtml}
    <div class="ent-summary">${e.summary}</div>
    <div class="ent-sec">Связи →</div>${out}
    <div class="ent-sec">← Упоминается</div>${inp}
    <div class="ent-sec">Из дневника</div>
    ${ments||'<div style="color:var(--ink3);font-size:12px;font-family:sans-serif">нет записей</div>'}
    <button class="ent-del" onclick="deleteEntity('${e.name.replace(/'/g,"\\'")}')">× Удалить из базы</button>`;
  document.getElementById('ent-modal').classList.add('open');
}
function closeEnt(){document.getElementById('ent-modal').classList.remove('open');}
async function deleteEntity(name){
  if(!confirm(`Удалить "${name}" из базы знаний?`)) return;
  const id=name.toLowerCase().replace(/[\s\-]+/g,'_').replace(/[^a-z0-9а-яё_]/g,'');
  await fetch(`/entities/${encodeURIComponent(id)}/delete`,{method:'POST'});
  closeEnt(); loadBase();
}


// ── Dialog helpers ────────────────────────────────────────────────────────────
function openDlg(id){document.getElementById(id).classList.add('open');}
function closeDlg(id){document.getElementById(id).classList.remove('open');}

// ── Auto-refresh journal (picks up Archivist updates) ─────────────────────────
// Reload whenever inbox count changes — including when it drops to 0 (processing done)
function fmtCountdown(lastTs, resetHours){
  if(!lastTs) return `сброс ${resetHours||24}ч`;
  const last=new Date(lastTs.replace(' ','T'));
  const due=new Date(last.getTime()+(resetHours||24)*3600*1000);
  const diff=due-Date.now();
  if(diff<=0) return 'сброс скоро';
  const h=Math.floor(diff/3600000);
  const m=Math.floor((diff%3600000)/60000);
  if(h>=1) return `сброс через ${h}ч ${m}м`;
  return `сброс через ${m}м`;
}
function tickCountdowns(){
  document.querySelectorAll('.reset-hint[data-reset-ts]').forEach(el=>{
    const ts=el.dataset.resetTs; const rh=parseInt(el.dataset.resetHours)||24;
    el.textContent='· '+fmtCountdown(ts,rh);
  });
}
setInterval(tickCountdowns,30000);

let _prevInboxCount = -1;
setInterval(async ()=>{
  try{
    const r=await fetch('/inbox'); const inbox=await r.json();
    const count=inbox.filter(i=>!i.type||i.type==='entry').length;
    if(count !== _prevInboxCount){
      _prevInboxCount=count;
      if(document.querySelector('#s-journal.active')){loadJournal();loadAsides();}
      if(document.querySelector('#s-missions.active')) loadMissions();
    }
  }catch(e){}
}, 5000);

// ── Pocket ───────────────────────────────────────────────────────────────────
function fmt(n){return n.toLocaleString('ru-RU',{minimumFractionDigits:0,maximumFractionDigits:2});}
async function loadPocket(){
  const d=await (await fetch('/pocket')).json();
  document.getElementById('pc-balance').textContent=fmt(d.balance)+' ₽';
  document.getElementById('pc-deferred').textContent=fmt(d.deferred)+' ₽';
  document.getElementById('pc-reserve-pct').value=d.reserve_pct;
  const DIR={p_income:'income',p_expense:'expense',p_deferred:'deferred',p_deferred_spend:'expense',p_adjust:'income',p_deferred_adjust:'deferred'};
  const LABEL={p_income:'+ доход',p_expense:'− расход',p_deferred:'→ резерв',p_deferred_spend:'← из резерва',p_adjust:'✎ корректировка баланса',p_deferred_adjust:'✎ корректировка резерва'};
  const SIGN={p_income:'+',p_expense:'−',p_deferred:'→',p_deferred_spend:'←',p_adjust:'±',p_deferred_adjust:'±'};
  document.getElementById('pocket-tx-list').innerHTML=d.transactions.length?
    d.transactions.map(t=>`
    <div class="pocket-tx-item">
      <div>
        <div class="pocket-tx-note">${LABEL[t.direction]||t.direction} · ${t.note}</div>
        <div class="pocket-tx-ts">${t.ts}</div>
      </div>
      <div class="pocket-tx-amount ${DIR[t.direction]||''}">${SIGN[t.direction]||''}${fmt(t.amount)} ₽</div>
    </div>`).join(''):
    '<div class="empty" style="padding:20px 0">Транзакций пока нет</div>';
}
async function savePocketCfg(){
  const pct=parseInt(document.getElementById('pc-reserve-pct').value)||20;
  await fetch('/pocket/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({reserve_pct:pct})});
  loadPocket();
}
function togglePocketForm(which){
  ['income','expense','deferred-spend','adjust'].forEach(f=>{
    const el=document.getElementById('pf-'+f);
    el.classList.toggle('open',f===which&&!el.classList.contains('open'));
  });
}
function closePocketForms(){
  ['income','expense','deferred-spend','adjust'].forEach(f=>document.getElementById('pf-'+f).classList.remove('open'));
}
async function submitPocketIncome(){
  const amount=parseFloat(document.getElementById('pf-income-amount').value)||0;
  const source=document.getElementById('pf-income-source').value.trim();
  if(!amount) return;
  await fetch('/pocket/income',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({amount,source})});
  document.getElementById('pf-income-amount').value='';
  document.getElementById('pf-income-source').value='';
  closePocketForms(); loadPocket();
}
async function submitPocketExpense(fromDeferred){
  const amountId=fromDeferred?'pf-ds-amount':'pf-expense-amount';
  const noteId=fromDeferred?'pf-ds-note':'pf-expense-note';
  const amount=parseFloat(document.getElementById(amountId).value)||0;
  const note=document.getElementById(noteId).value.trim();
  if(!amount) return;
  await fetch('/pocket/expense',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({amount,note,from_deferred:fromDeferred})});
  document.getElementById(amountId).value='';
  document.getElementById(noteId).value='';
  closePocketForms(); loadPocket();
}

async function submitPocketAdjust(){
  const amount=parseFloat(document.getElementById('pf-adjust-amount').value);
  const note=document.getElementById('pf-adjust-note').value.trim();
  const target=document.getElementById('pf-adjust-target').value;
  if(!amount) return;
  await fetch('/pocket/adjust',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({amount,note,target})});
  document.getElementById('pf-adjust-amount').value='';
  document.getElementById('pf-adjust-note').value='';
  closePocketForms(); loadPocket();
}

// ── Settings ─────────────────────────────────────────────────────────────────
async function checkApiStatus(){
  try{
    const d=await (await fetch('/config/status')).json();
    const el=document.getElementById('nav-api-status');
    const active=d.active;
    const labels={anthropic:'✓ Claude активен',gigachat:'✓ GigaChat активен',none:'⚠ ИИ не настроен'};
    const colors={anthropic:'#6a9940',gigachat:'#6a9940',none:'#c06030'};
    if(el){el.textContent=labels[active]||labels.none; el.style.color=colors[active]||colors.none;}
    return active!=='none';
  }catch(e){ return false; }
}
function openSettings(){
  fetch('/config/status').then(r=>r.json()).then(d=>{
    const st=document.getElementById('settings-key-status');
    const msgs={anthropic:'✓ Anthropic Claude активен',gigachat:'✓ GigaChat активен',none:'ИИ не настроен — Архивариус не работает автономно'};
    st.textContent=msgs[d.active]||msgs.none;
    st.className='settings-status '+(d.active!=='none'?'ok':'missing');
  });
  document.getElementById('settings-modal').classList.add('open');
}
function closeSettings(){ document.getElementById('settings-modal').classList.remove('open'); }
async function saveApiKey(){
  const key=document.getElementById('settings-api-key').value.trim();
  if(!key) return;
  const d=await (await fetch('/config/api-key',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key})})).json();
  if(d.ok){
    document.getElementById('settings-api-key').value='';
    checkApiStatus(); openSettings(); processPendingInbox();
  }
}
async function saveGigaChat(){
  const key=document.getElementById('settings-gc-key').value.trim();
  if(!key) return;
  const d=await (await fetch('/config/gigachat',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({gigachat_key:key,gigachat_scope:'GIGACHAT_API_PERS'})})).json();
  if(d.ok){
    document.getElementById('settings-gc-key').value='';
    checkApiStatus(); openSettings(); processPendingInbox();
  }
}
async function processPendingInbox(){
  try{
    const d=await (await fetch('/inbox/process-pending',{method:'POST'})).json();
    if(d.ok && d.started>0){
      // Poll until inbox clears
      let tries=0;
      const iv=setInterval(async()=>{
        tries++;
        const inbox=await (await fetch('/inbox')).json();
        const rem=inbox.filter(i=>!i.type||i.type==='entry').length;
        if(rem===0||tries>24){ clearInterval(iv); loadJournal(); loadAsides(); }
      },5000);
    }
  }catch(e){}
}

// ── Init ─────────────────────────────────────────────────────────────────────
loadJournal(); loadAsides(); checkApiStatus();
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
def root(): return HTML
