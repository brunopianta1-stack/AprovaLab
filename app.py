import os, json, re, time, sqlite3, hashlib, random
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename
from pypdf import PdfReader
from openai import OpenAI
from spaced_repetition import next_review, memory_snapshot
from manual_import import manual_import_bp
BASE = Path(__file__).resolve().parent

# Arquivos internos do aplicativo permanecem na pasta de instalação.
# Dados do usuário ficam no AppData, onde o Windows permite gravação.
LOCAL_APPDATA = Path(os.environ.get(
    "LOCALAPPDATA",
    Path.home() / "AppData" / "Local"
))

DATA_DIR = LOCAL_APPDATA / "AprovaLab"
DATA_DIR.mkdir(parents=True, exist_ok=True)

UPLOADS = DATA_DIR / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)

DB = DATA_DIR / "aprovallab_v5.db"

app = Flask(__name__)
app.secret_key = "aprovallab-local-v5"
app.register_blueprint(manual_import_bp)

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS exams(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      institution TEXT DEFAULT '',
      board TEXT DEFAULT 'CEBRASPE',
      target_date TEXT DEFAULT '',
      description TEXT DEFAULT '',
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS subjects(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      exam_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      weight REAL DEFAULT 1,
      target_accuracy REAL DEFAULT 80,
      color_tag TEXT DEFAULT '',
      created_at TEXT NOT NULL,
      UNIQUE(exam_id, name)
    );

    CREATE TABLE IF NOT EXISTS documents(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      hash TEXT UNIQUE NOT NULL,
      pages INTEGER DEFAULT 0,
      content TEXT NOT NULL,
      subject_id INTEGER,
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS notebooks(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      description TEXT DEFAULT '',
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS questions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      doc_id INTEGER,
      subject_id INTEGER,
      notebook_id INTEGER,
      statement TEXT NOT NULL,
      answer TEXT NOT NULL,
      explanation TEXT NOT NULL,
      basis TEXT NOT NULL,
      page INTEGER,
      topic TEXT DEFAULT '',
      difficulty TEXT DEFAULT '',
      favorite INTEGER DEFAULT 0,
      created_at TEXT NOT NULL,
      UNIQUE(doc_id, statement)
    );

    CREATE TABLE IF NOT EXISTS reviews(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      question_id INTEGER NOT NULL,
      choice TEXT NOT NULL,
      correct INTEGER NOT NULL,
      confidence TEXT NOT NULL,
      seconds REAL DEFAULT 0,
      mode TEXT DEFAULT '',
      reviewed_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS schedule(
      question_id INTEGER PRIMARY KEY,
      interval_days INTEGER DEFAULT 0,
      ease REAL DEFAULT 2.5,
      repetitions INTEGER DEFAULT 0,
      lapses INTEGER DEFAULT 0,
      due_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      exam_id INTEGER,
      mode TEXT,
      total INTEGER,
      correct INTEGER,
      duration REAL,
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS study_plan(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      exam_id INTEGER NOT NULL,
      subject_id INTEGER NOT NULL,
      priority REAL DEFAULT 0,
      recommended_questions INTEGER DEFAULT 10,
      reason TEXT DEFAULT '',
      updated_at TEXT NOT NULL,
      UNIQUE(exam_id, subject_id)
    );
    """)

    migrations = {
        "exams": [("new_cards_per_day", "INTEGER DEFAULT 20"), ("reviews_per_day", "INTEGER DEFAULT 100"), ("leech_threshold", "INTEGER DEFAULT 8")],
        "subjects": [("parent_id", "INTEGER")],
        "reviews": [("rating", "TEXT DEFAULT ''")],
        "schedule": [("state", "TEXT DEFAULT 'new'"), ("stability", "REAL DEFAULT 0.4"), ("difficulty_score", "REAL DEFAULT 5"), ("suspended", "INTEGER DEFAULT 0")],
    }
    for table, columns in migrations.items():
        present = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in present:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    c.execute("UPDATE schedule SET state='review' WHERE repetitions>0 AND state='new'")

    c.execute("""
      INSERT OR IGNORE INTO notebooks(name,description,created_at)
      VALUES(?,?,?)
    """, ("Geral", "Questões gerais", datetime.now().isoformat()))

    c.commit()
    c.close()

def scalar(sql, args=()):
    c=conn()
    r=c.execute(sql,args).fetchone()
    c.close()
    return r[0] if r else 0

def summary(exam_id=None):
    if exam_id:
        q_filter = " WHERE s.exam_id=? "
        questions = scalar("""SELECT COUNT(*) FROM questions q JOIN subjects s ON s.id=q.subject_id WHERE s.exam_id=?""",(exam_id,))
        reviews = scalar("""SELECT COUNT(*) FROM reviews r JOIN questions q ON q.id=r.question_id JOIN subjects s ON s.id=q.subject_id WHERE s.exam_id=?""",(exam_id,))
        correct = scalar("""SELECT COUNT(*) FROM reviews r JOIN questions q ON q.id=r.question_id JOIN subjects s ON s.id=q.subject_id WHERE s.exam_id=? AND r.correct=1""",(exam_id,))
        due = scalar("""SELECT COUNT(*) FROM schedule sc JOIN questions q ON q.id=sc.question_id JOIN subjects s ON s.id=q.subject_id WHERE s.exam_id=? AND sc.due_at<=? AND COALESCE(sc.suspended,0)=0""",(exam_id,datetime.now().isoformat()))
        subjects = scalar("SELECT COUNT(*) FROM subjects WHERE exam_id=?",(exam_id,))
    else:
        questions=scalar("SELECT COUNT(*) FROM questions")
        reviews=scalar("SELECT COUNT(*) FROM reviews")
        correct=scalar("SELECT COUNT(*) FROM reviews WHERE correct=1")
        due=scalar("SELECT COUNT(*) FROM schedule WHERE due_at<=? AND COALESCE(suspended,0)=0",(datetime.now().isoformat(),))
        subjects=scalar("SELECT COUNT(*) FROM subjects")
    return {
        "questions":questions,
        "reviews":reviews,
        "accuracy":round(correct*100/reviews,1) if reviews else 0,
        "due":due,
        "subjects":subjects
    }

def exam_subject_stats(exam_id):
    c=conn()
    rows=c.execute("""
    SELECT
      s.id, s.name, s.weight, s.target_accuracy,
      COUNT(DISTINCT q.id) AS questions,
      COUNT(r.id) AS attempts,
      COALESCE(AVG(r.correct)*100,0) AS accuracy,
      COALESCE(AVG(r.seconds),0) AS avg_time,
      COALESCE(SUM(CASE WHEN r.correct=0 THEN 1 ELSE 0 END),0) AS errors,
      COALESCE(SUM(CASE WHEN sc.due_at<=? THEN 1 ELSE 0 END),0) AS due
    FROM subjects s
    LEFT JOIN questions q ON q.subject_id=s.id
    LEFT JOIN reviews r ON r.question_id=q.id
    LEFT JOIN schedule sc ON sc.question_id=q.id
    WHERE s.exam_id=?
    GROUP BY s.id
    ORDER BY s.name
    """,(datetime.now().isoformat(),exam_id)).fetchall()
    c.close()

    out=[]
    for r in rows:
        d=dict(r)
        target=float(d["target_accuracy"] or 80)
        acc=float(d["accuracy"] or 0)
        attempts=int(d["attempts"] or 0)
        errors=int(d["errors"] or 0)
        due=int(d["due"] or 0)
        weight=float(d["weight"] or 1)

        gap=max(0,target-acc)/100
        no_data=1 if attempts==0 else 0
        priority=(gap*8)+(errors/max(attempts,1))*5+min(due,10)*0.25+weight*0.7+no_data*3
        mastery=max(0,min(100,acc-(errors*0.15)))
        d["priority"]=round(priority,2)
        d["mastery"]=round(mastery,1)
        out.append(d)
    out.sort(key=lambda x:x["priority"],reverse=True)
    return out

def refresh_plan(exam_id):
    stats=exam_subject_stats(exam_id)
    c=conn()
    for s in stats:
        if s["attempts"]==0:
            reason="Sem dados suficientes: precisa de diagnóstico inicial."
            rec=max(10,round(10*s["weight"]))
        elif s["accuracy"] < s["target_accuracy"]:
            gap=round(s["target_accuracy"]-s["accuracy"],1)
            reason=f"Acerto {gap} p.p. abaixo da meta e {s['errors']} erros acumulados."
            rec=max(10,min(30,round(10+s["priority"])))
        elif s["due"]>0:
            reason=f"{s['due']} revisões vencidas apesar de desempenho adequado."
            rec=min(25,max(8,s["due"]))
        else:
            reason="Desempenho estável; manter exposição periódica."
            rec=8
        c.execute("""
        INSERT INTO study_plan(exam_id,subject_id,priority,recommended_questions,reason,updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(exam_id,subject_id) DO UPDATE SET
        priority=excluded.priority,
        recommended_questions=excluded.recommended_questions,
        reason=excluded.reason,
        updated_at=excluded.updated_at
        """,(exam_id,s["id"],s["priority"],rec,reason,datetime.now().isoformat()))
    c.commit(); c.close()

def qdict(r):
    d=dict(r); d["db_id"]=d["id"]; return d

def select_questions(mode,limit=20,exam_id=None,subject_id=None):
    c=conn()
    params=[]
    where=[]
    if exam_id:
        where.append("s.exam_id=?"); params.append(exam_id)
    if subject_id:
        where.append("q.subject_id=?"); params.append(subject_id)
    wf=" WHERE "+" AND ".join(where) if where else ""

    if mode=="due":
        sql=f"""SELECT q.* FROM questions q
                JOIN subjects s ON s.id=q.subject_id
                JOIN schedule sc ON sc.question_id=q.id
                {wf + (' AND ' if wf else ' WHERE ') + 'sc.due_at<=? AND COALESCE(sc.suspended,0)=0'}
                ORDER BY sc.due_at ASC, sc.lapses DESC LIMIT ?"""
        rows=c.execute(sql,tuple(params+[datetime.now().isoformat(),limit])).fetchall()
    elif mode=="errors":
        sql=f"""SELECT q.*, SUM(CASE WHEN r.correct=0 THEN 1 ELSE 0 END) errors
                FROM questions q JOIN subjects s ON s.id=q.subject_id
                JOIN reviews r ON r.question_id=q.id {wf}
                GROUP BY q.id HAVING errors>0 ORDER BY errors DESC LIMIT ?"""
        rows=c.execute(sql,tuple(params+[limit])).fetchall()
    elif mode=="subject":
        rows=c.execute("""SELECT * FROM questions WHERE subject_id=? ORDER BY created_at DESC LIMIT ?""",(subject_id,limit)).fetchall()
    else:
        sql=f"""SELECT q.*, COALESCE(sc.lapses,0) lapses, sc.due_at,
                COUNT(r.id) attempts, COALESCE(AVG(r.correct),0.5) accuracy,
                COALESCE(AVG(CASE WHEN r.confidence='Certeza' THEN 1.0
                WHEN r.confidence='Dúvida' THEN .6 ELSE .2 END),.5) confidence_score
                FROM questions q JOIN subjects s ON s.id=q.subject_id
                LEFT JOIN schedule sc ON sc.question_id=q.id
                LEFT JOIN reviews r ON r.question_id=q.id {wf}
                GROUP BY q.id"""
        rows=c.execute(sql,tuple(params)).fetchall()
        now=datetime.now(); scored=[]
        for r in rows:
            d=dict(r); due_bonus=0
            try:
                due=datetime.fromisoformat(d["due_at"]) if d.get("due_at") else now
                due_bonus=max(0,(now-due).total_seconds()/86400)*1.5
            except: pass
            score=due_bonus+(1-float(d["accuracy"]))*8+(1-float(d["confidence_score"]))*3+min(int(d["lapses"]),5)*1.2+(2.5 if int(d["attempts"])==0 else 0)+random.random()*.3
            scored.append((score,d))
        scored.sort(reverse=True,key=lambda x:x[0])
        rows=[x[1] for x in scored[:limit]]
    c.close()
    return [qdict(r) if not isinstance(r,dict) else (r|{"db_id":r["id"]}) for r in rows]

def mixed_exam_questions(exam_id,total):
    stats=exam_subject_stats(exam_id)
    if not stats: return []
    total_weight=sum(max(float(s["weight"]),0.1) for s in stats)
    picks=[]
    c=conn()
    for s in stats:
        quota=max(1,round(total*(float(s["weight"])/total_weight)))
        rows=c.execute("""SELECT q.* FROM questions q WHERE q.subject_id=?
                          ORDER BY RANDOM() LIMIT ?""",(s["id"],quota)).fetchall()
        picks += [qdict(r) for r in rows]
    c.close()
    random.shuffle(picks)
    return picks[:total]

def record_review(qid, choice, confidence, seconds, mode):
    c = conn()

    q = c.execute(
        "SELECT answer FROM questions WHERE id=?",
        (qid,)
    ).fetchone()

    if not q:
        c.close()
        return False, None

    ok = choice == q["answer"]
    now = datetime.now()

    c.execute("""
        INSERT INTO reviews(
            question_id,
            choice,
            correct,
            confidence,
            seconds,
            mode,
            reviewed_at
        )
        VALUES(?,?,?,?,?,?,?)
    """, (
        qid,
        choice,
        int(ok),
        confidence,
        seconds,
        mode,
        now.isoformat()
    ))

    c.commit()
    review_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.close()
    return ok, review_id

@app.route("/")
def home():
    c=conn(); exams=c.execute("SELECT * FROM exams ORDER BY created_at DESC").fetchall(); c.close()
    if len(exams) == 1:
        return redirect(url_for("exam_dashboard", exam_id=exams[0]["id"]))
    return render_template("home.html",exams=exams)

@app.route("/exam/new",methods=["GET","POST"])
def exam_new():
    if request.method=="POST":
        c=conn()
        try:
            name=request.form["name"].strip()
            if not name:
                raise ValueError("Informe o nome do concurso.")
            c.execute("""INSERT INTO exams(name,institution,board,target_date,description,created_at)
                         VALUES(?,?,?,?,?,?)""",
                      (name,request.form.get("institution","").strip(),
                       request.form.get("board","CEBRASPE").strip(),request.form.get("target_date",""),
                       request.form.get("description","").strip(),datetime.now().isoformat()))
            c.commit()
            eid=c.execute("SELECT id FROM exams WHERE name=?",(name,)).fetchone()["id"]
            c.close()
            return redirect(url_for("exam_dashboard",exam_id=eid))
        except Exception as e:
            c.close()
            flash(str(e),"error")
    return render_template("exam_new.html")

@app.route("/answer", methods=["POST"])
def answer():
    data = request.get_json()

    qid = int(data["qid"])

    ok, review_id = record_review(
        qid,
        data["choice"],
        data["confidence"],
        float(data.get("seconds", 0)),
        data.get("mode", "")
    )

    c = conn()
    q = c.execute(
        "SELECT * FROM questions WHERE id=?",
        (qid,)
    ).fetchone()
    c.close()

    return jsonify({
        "correct": ok,
        "answer": q["answer"],
        "explanation": q["explanation"],
        "basis": q["basis"],
        "page": q["page"],
        "review_id": review_id
    })

@app.route("/rate", methods=["POST"])
def rate_review():
    data=request.get_json() or {}
    review_id=int(data.get("review_id",0)); rating=data.get("rating","")
    c=conn()
    row=c.execute("""SELECT r.question_id,r.rating,s.exam_id FROM reviews r JOIN questions q ON q.id=r.question_id
                      JOIN subjects s ON s.id=q.subject_id WHERE r.id=?""",(review_id,)).fetchone()
    if not row:
        c.close(); return jsonify({"error":"Revisão não encontrada."}),404
    exam=c.execute("SELECT leech_threshold FROM exams WHERE id=?",(row["exam_id"],)).fetchone()
    try:
        if row["rating"]:
            memory=memory_snapshot(c,row["question_id"]); c.close()
            return jsonify({"ok":True,"memory":memory})
        c.execute("UPDATE reviews SET rating=? WHERE id=?",(rating,review_id))
        memory=next_review(c,row["question_id"],rating,int(exam["leech_threshold"] or 8))
        c.commit(); c.close(); return jsonify({"ok":True,"memory":memory})
    except ValueError as e:
        c.close(); return jsonify({"error":str(e)}),400

@app.route("/exam/<int:exam_id>")
def exam_dashboard(exam_id):
    refresh_plan(exam_id)
    c=conn()
    exam=c.execute("SELECT * FROM exams WHERE id=?",(exam_id,)).fetchone()
    if not exam:
        c.close()
        flash("Concurso não encontrado.","error")
        return redirect(url_for("home"))
    plan=c.execute("""SELECT sp.*,s.name FROM study_plan sp JOIN subjects s ON s.id=sp.subject_id
                      WHERE sp.exam_id=? ORDER BY sp.priority DESC""",(exam_id,)).fetchall()
    subject_rows=c.execute("""SELECT s.*,COUNT(q.id) questions
                              FROM subjects s LEFT JOIN questions q ON q.subject_id=s.id
                              WHERE s.exam_id=? GROUP BY s.id ORDER BY s.name""",(exam_id,)).fetchall()
    question_ids=c.execute("""SELECT q.id FROM questions q JOIN subjects s ON s.id=q.subject_id
                              WHERE s.exam_id=?""",(exam_id,)).fetchall()
    c.close()
    stats=exam_subject_stats(exam_id)
    chart=[{"label":s["name"],"value":s["mastery"]} for s in stats]
    memory={"learning":0,"consolidating":0,"mastered":0}
    c=conn()
    for row in question_ids:
        snap=memory_snapshot(c,row["id"])
        css=snap["memory_class"]
        if css in ("mastered90","mastered95"):
            memory["mastered"]+=1
        elif css=="consolidating":
            memory["consolidating"]+=1
        else:
            memory["learning"]+=1
    c.close()
    return render_template("exam_dashboard.html",exam=exam,s=summary(exam_id),stats=stats,
                           plan=plan,chart=chart,subjects=subject_rows,memory=memory)

@app.route("/exam/<int:exam_id>/quick-sim",methods=["POST"])
def quick_simulator(exam_id):
    total=max(5,min(100,int(request.form.get("total",20))))
    subject_id=request.form.get("subject_id",type=int)
    question_mode=request.form.get("question_mode","adaptive")
    if question_mode in ("due","errors","adaptive"):
        qs=select_questions(question_mode,total,exam_id,subject_id)
    elif subject_id:
        c=conn()
        rows=c.execute("""SELECT q.* FROM questions q JOIN subjects s ON s.id=q.subject_id
                          WHERE q.subject_id=? AND s.exam_id=? ORDER BY RANDOM() LIMIT ?""",
                       (subject_id,exam_id,total)).fetchall()
        c.close()
        qs=[qdict(r) for r in rows]
    else:
        qs=mixed_exam_questions(exam_id,total)
    if not qs:
        flash("Cadastre ou importe questões antes de iniciar o simulado.","error")
        return redirect(url_for("exam_dashboard",exam_id=exam_id))
    return redirect(url_for("quiz",exam_id=exam_id,mode="ids",
                            ids=",".join(str(q["id"]) for q in qs)))

@app.route("/exam/<int:exam_id>/errors")
def error_notebook(exam_id):
    c=conn()
    exam=c.execute("SELECT * FROM exams WHERE id=?",(exam_id,)).fetchone()
    rows=c.execute("""SELECT q.id,q.statement,q.topic,q.difficulty,s.name subject_name,
                      COUNT(r.id) attempts,SUM(CASE WHEN r.correct=0 THEN 1 ELSE 0 END) errors,
                      ROUND(AVG(r.correct)*100,1) accuracy
                      FROM questions q JOIN subjects s ON s.id=q.subject_id
                      JOIN reviews r ON r.question_id=q.id
                      WHERE s.exam_id=? GROUP BY q.id HAVING errors>0
                      ORDER BY errors DESC,accuracy ASC""",(exam_id,)).fetchall()
    c.close()
    return render_template("errors.html",exam=exam,rows=rows)

@app.route("/exam/<int:exam_id>/statistics")
def statistics(exam_id):
    c=conn()
    exam=c.execute("SELECT * FROM exams WHERE id=?",(exam_id,)).fetchone()
    sessions=c.execute("""SELECT * FROM sessions WHERE exam_id=?
                         ORDER BY created_at DESC LIMIT 12""",(exam_id,)).fetchall()
    retention=c.execute("""SELECT substr(r.reviewed_at,1,10) day,COUNT(*) total,SUM(r.correct) correct
                            FROM reviews r JOIN questions q ON q.id=r.question_id JOIN subjects s ON s.id=q.subject_id
                            WHERE s.exam_id=? GROUP BY day ORDER BY day DESC LIMIT 30""",(exam_id,)).fetchall()
    maturity=c.execute("""SELECT SUM(CASE WHEN COALESCE(sc.state,'new') IN ('new','learning','relearning') THEN 1 ELSE 0 END) learning,
                           SUM(CASE WHEN sc.state='review' AND sc.interval_days<21 THEN 1 ELSE 0 END) young,
                           SUM(CASE WHEN sc.state='review' AND sc.interval_days>=21 THEN 1 ELSE 0 END) mature,
                           SUM(CASE WHEN COALESCE(sc.suspended,0)=1 THEN 1 ELSE 0 END) leeches
                           FROM questions q JOIN subjects s ON s.id=q.subject_id LEFT JOIN schedule sc ON sc.question_id=q.id WHERE s.exam_id=?""",(exam_id,)).fetchone()
    forecast=[]
    for offset in range(14):
        day=(datetime.now()+timedelta(days=offset)).date().isoformat()
        count=c.execute("""SELECT COUNT(*) FROM schedule sc JOIN questions q ON q.id=sc.question_id JOIN subjects s ON s.id=q.subject_id
                           WHERE s.exam_id=? AND substr(sc.due_at,1,10)=? AND COALESCE(sc.suspended,0)=0""",(exam_id,day)).fetchone()[0]
        forecast.append({"label":datetime.fromisoformat(day).strftime("%d/%m"),"value":count})
    c.close()
    stats=exam_subject_stats(exam_id)
    return render_template("statistics.html",exam=exam,s=summary(exam_id),stats=stats,sessions=sessions,
                           chart=[{"label":x["name"],"value":x["accuracy"]} for x in stats],
                           retention=[{"label":x["day"][5:],"value":round(100*x["correct"]/x["total"])} for x in reversed(retention)],
                           maturity=dict(maturity),forecast=forecast)

@app.route("/exam/<int:exam_id>/leeches", methods=["POST"])
def restore_leeches(exam_id):
    c=conn(); c.execute("""UPDATE schedule SET suspended=0,lapses=0,state='relearning',due_at=? WHERE question_id IN
                           (SELECT q.id FROM questions q JOIN subjects s ON s.id=q.subject_id WHERE s.exam_id=?)""",
                        (datetime.now().isoformat(),exam_id))
    c.commit(); c.close(); flash("Cartões sanguessuga reativados para reaprendizado.","ok")
    return redirect(url_for("statistics",exam_id=exam_id))

@app.route("/exam/<int:exam_id>/import")
def import_hub(exam_id):
    c=conn()
    exam=c.execute("SELECT * FROM exams WHERE id=?",(exam_id,)).fetchone()
    rows=c.execute("""SELECT s.*,COUNT(q.id) questions FROM subjects s
                      LEFT JOIN questions q ON q.subject_id=s.id
                      WHERE s.exam_id=? GROUP BY s.id ORDER BY s.name""",(exam_id,)).fetchall()
    c.close()
    return render_template("import_hub.html",exam=exam,rows=rows)

@app.route("/exam/<int:exam_id>/settings",methods=["GET","POST"])
def settings(exam_id):
    c=conn()
    exam=c.execute("SELECT * FROM exams WHERE id=?",(exam_id,)).fetchone()
    if request.method=="POST" and exam:
        try:
            name=request.form.get("name","").strip()
            if not name:
                raise ValueError("Informe o nome do concurso.")
            c.execute("""UPDATE exams SET name=?,institution=?,board=?,target_date=?,description=?,new_cards_per_day=?,reviews_per_day=?,leech_threshold=?
                         WHERE id=?""",(name,request.form.get("institution","").strip(),
                         request.form.get("board","").strip(),request.form.get("target_date",""),
                         request.form.get("description","").strip(),
                         max(1,int(request.form.get("new_cards_per_day",20))),
                         max(1,int(request.form.get("reviews_per_day",100))),
                         max(2,int(request.form.get("leech_threshold",8))),exam_id))
            c.commit()
            flash("Configurações atualizadas.","ok")
            exam=c.execute("SELECT * FROM exams WHERE id=?",(exam_id,)).fetchone()
        except Exception as e:
            flash(str(e),"error")
    c.close()
    return render_template("settings.html",exam=exam)

@app.route("/exam/<int:exam_id>/subjects",methods=["GET","POST"])
def subjects(exam_id):
    c=conn()
    exam=c.execute("SELECT * FROM exams WHERE id=?",(exam_id,)).fetchone()
    if request.method=="POST":
        try:
            c.execute("""INSERT INTO subjects(exam_id,name,weight,target_accuracy,parent_id,created_at)
                         VALUES(?,?,?,?,?,?)""",(exam_id,request.form["name"].strip(),
                         float(request.form.get("weight",1)),float(request.form.get("target_accuracy",80)),
                         request.form.get("parent_id",type=int),datetime.now().isoformat()))
            c.commit(); flash("Disciplina criada.","ok")
        except Exception as e: flash(str(e),"error")
    rows=c.execute("""SELECT s.*,p.name parent_name,COUNT(DISTINCT d.id) docs,COUNT(DISTINCT q.id) questions
                      FROM subjects s LEFT JOIN documents d ON d.subject_id=s.id
                      LEFT JOIN questions q ON q.subject_id=s.id
                      LEFT JOIN subjects p ON p.id=s.parent_id
                      WHERE s.exam_id=? GROUP BY s.id ORDER BY COALESCE(p.name,s.name),s.parent_id,s.name""",(exam_id,)).fetchall()
    c.close()
    return render_template("subjects.html",exam=exam,rows=rows)

@app.route("/exam/<int:exam_id>/upload/<int:subject_id>",methods=["POST"])
def upload_subject_pdf(exam_id,subject_id):
    f=request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        flash("Selecione um PDF válido.","error"); return redirect(url_for("subjects",exam_id=exam_id))
    name=secure_filename(f.filename); path=UPLOADS/name; f.save(path)
    try:
        raw=path.read_bytes(); digest=hashlib.sha256(raw).hexdigest(); reader=PdfReader(str(path)); parts=[]
        for i,p in enumerate(reader.pages,1):
            parts.append(f"\n--- PÁGINA {i} ---\n{p.extract_text() or ''}")
        content="\n".join(parts).strip()
        if not content: raise ValueError("PDF sem texto extraível.")
        c=conn()
        c.execute("""INSERT OR IGNORE INTO documents(name,hash,pages,content,subject_id,created_at)
                     VALUES(?,?,?,?,?,?)""",(name,digest,len(reader.pages),content,subject_id,datetime.now().isoformat()))
        c.commit(); c.close(); flash("PDF associado à disciplina.","ok")
    except Exception as e: flash(f"Erro no PDF: {e}","error")
    return redirect(url_for("subjects",exam_id=exam_id))

@app.route("/exam/<int:exam_id>/generate/<int:subject_id>",methods=["GET","POST"])
def generate_subject(exam_id,subject_id):
    c=conn()
    exam=c.execute("SELECT * FROM exams WHERE id=?",(exam_id,)).fetchone()
    subject=c.execute("SELECT * FROM subjects WHERE id=? AND exam_id=?",(subject_id,exam_id)).fetchone()
    docs=c.execute("SELECT * FROM documents WHERE subject_id=? ORDER BY created_at DESC",(subject_id,)).fetchall()
    c.close()
    if request.method=="POST":
        key=request.form.get("api_key","").strip()
        qty=max(5,min(50,int(request.form.get("qty",10))))
        difficulty=request.form.get("difficulty","Mista")
        focus=", ".join(request.form.getlist("focus")) or "conteúdo geral"
        doc_ids=[int(x) for x in request.form.getlist("doc_ids")]
        if not key or not doc_ids:
            flash("Informe a API e selecione ao menos um PDF.","error")
            return redirect(request.url)
        c=conn()
        placeholders=",".join("?"*len(doc_ids))
        selected=c.execute(f"SELECT * FROM documents WHERE id IN ({placeholders})",doc_ids).fetchall()
        c.close()
        source="\n\n".join([d["content"] for d in selected])
        try:
            client=OpenAI(api_key=key)
            prompt=f"""Você é elaborador especializado em provas {exam['board']}.
Crie EXATAMENTE {qty} itens CERTO/ERRADO para a disciplina {subject['name']},
usando EXCLUSIVAMENTE os PDFs abaixo.
Dificuldade: {difficulty}. Focos: {focus}.
Regras: afirmações autossuficientes; distribuição equilibrada e imprevisível; erro deve ser material;
não use conhecimento externo, não atualize o PDF, não invente jurisprudência; explique pelo PDF;
basis deve indicar dispositivo/trecho e page deve indicar a página; evite duplicidade.
Retorne SOMENTE JSON:
{{"questions":[{{"statement":"...","answer":"Certo","explanation":"...","basis":"...","page":1,"topic":"...","difficulty":"Média"}}]}}
FONTES:
{source}"""
            r=client.responses.create(model="gpt-5.6-luna",input=prompt)
            raw=re.sub(r"^```(?:json)?\s*|\s*```$","",r.output_text.strip())
            qs=json.loads(raw)["questions"]
            if len(qs)!=qty: raise ValueError(f"Foram geradas {len(qs)} questões; esperadas {qty}.")
            c=conn(); ids=[]
            doc_id=doc_ids[0]
            for q in qs:
                c.execute("""INSERT OR IGNORE INTO questions(doc_id,subject_id,notebook_id,statement,answer,explanation,basis,page,topic,difficulty,created_at)
                             VALUES(?,?,NULL,?,?,?,?,?,?,?,?)""",
                          (doc_id,subject_id,q["statement"],q["answer"],q["explanation"],q["basis"],q.get("page"),q.get("topic",""),q.get("difficulty",""),datetime.now().isoformat()))
                row=c.execute("SELECT id FROM questions WHERE doc_id=? AND statement=?",(doc_id,q["statement"])).fetchone()
                if row:
                    ids.append(row["id"])
                    c.execute("INSERT OR IGNORE INTO schedule(question_id,due_at) VALUES(?,?)",(row["id"],datetime.now().isoformat()))
            c.commit(); c.close()
            return redirect(url_for("quiz",exam_id=exam_id,mode="ids",ids=",".join(map(str,ids))))
        except Exception as e:
            flash(f"Falha na geração: {e}","error")
    return render_template("generate_subject.html",exam=exam,subject=subject,docs=docs)

@app.route("/exam/<int:exam_id>/simulado",methods=["GET","POST"])
def exam_simulator(exam_id):
    c=conn(); exam=c.execute("SELECT * FROM exams WHERE id=?",(exam_id,)).fetchone(); c.close()
    if request.method=="POST":
        total=max(10,min(200,int(request.form.get("total",50))))
        qs=mixed_exam_questions(exam_id,total)
        if not qs:
            flash("Ainda não há questões suficientes cadastradas.","error")
            return redirect(request.url)
        ids=",".join(str(q["id"]) for q in qs)
        return redirect(url_for("quiz",exam_id=exam_id,mode="ids",ids=ids))
    return render_template("simulator.html",exam=exam)

@app.route("/exam/<int:exam_id>/cycle")
def study_cycle(exam_id):
    refresh_plan(exam_id)
    c=conn()
    exam=c.execute("SELECT * FROM exams WHERE id=?",(exam_id,)).fetchone()
    rows=c.execute("""SELECT sp.*,s.name FROM study_plan sp JOIN subjects s ON s.id=sp.subject_id
                      WHERE sp.exam_id=? ORDER BY sp.priority DESC""",(exam_id,)).fetchall()
    c.close()
    total=sum(r["recommended_questions"] for r in rows) or 1
    cycle=[]
    for r in rows:
        share=round(r["recommended_questions"]/total*100)
        cycle.append(dict(r)|{"share":share})
    return render_template("cycle.html",exam=exam,cycle=cycle)

@app.route("/exam/<int:exam_id>/quiz/<mode>")
def quiz(exam_id,mode):
    ids=request.args.get("ids","")
    subject_id=request.args.get("subject_id",type=int)
    c=conn()
    limits=c.execute("SELECT new_cards_per_day,reviews_per_day FROM exams WHERE id=?",(exam_id,)).fetchone()
    today=datetime.now().date().isoformat()
    reviewed_today=c.execute("""SELECT COUNT(*) FROM reviews r JOIN questions q ON q.id=r.question_id JOIN subjects s ON s.id=q.subject_id
                                WHERE s.exam_id=? AND substr(r.reviewed_at,1,10)=?""",(exam_id,today)).fetchone()[0]
    c.close()
    daily_review_limit=max(0,int(limits["reviews_per_day"] or 100)-reviewed_today) if limits else 20
    new_seen_today=scalar("""SELECT COUNT(DISTINCT r.question_id) FROM reviews r JOIN questions q ON q.id=r.question_id
                             JOIN subjects s ON s.id=q.subject_id WHERE s.exam_id=? AND substr(r.reviewed_at,1,10)=?
                             AND NOT EXISTS(SELECT 1 FROM reviews old WHERE old.question_id=r.question_id AND old.reviewed_at<r.reviewed_at)""",(exam_id,today))
    available_new=max(0,int(limits["new_cards_per_day"] or 20)-new_seen_today) if limits else 20
    if mode=="ids":
        wanted=[int(x) for x in ids.split(",") if x.isdigit()]
        if wanted:
            c=conn(); placeholders=",".join("?"*len(wanted))
            rows=c.execute(f"""SELECT q.*,s.name subject_name FROM questions q
                               LEFT JOIN subjects s ON s.id=q.subject_id
                               WHERE q.id IN ({placeholders})""",wanted).fetchall()
            c.close(); qs=[qdict(r) for r in rows]
        else: qs=[]
    elif mode=="subject":
        qs=select_questions("subject",min(100,max(1,daily_review_limit)),exam_id,subject_id)
    else:
        qs=select_questions(mode,min(20,max(1,daily_review_limit)),exam_id)
    if not qs:
        flash("Não há questões disponíveis para este modo.","error")
        return redirect(url_for("exam_dashboard",exam_id=exam_id))

    c = conn()

    for q in qs:
        memory = memory_snapshot(c, q["db_id"])

        q["memory_label"] = memory["memory_label"]
        q["memory_class"] = memory["memory_class"]
        q["memory_accuracy"] = memory["accuracy"]
        q["memory_attempts"] = memory["attempts"]
        q["memory_due_label"] = memory["due_label"]

    c.close()
    if mode != "ids":
        kept=[]; new_count=0
        for q in qs:
            if q["memory_class"] == "new":
                if new_count >= available_new: continue
                new_count += 1
            kept.append(q)
        qs=kept
    if not qs:
        flash("Você atingiu o limite diário configurado. Ajuste-o em Configurações se desejar continuar.","ok")
        return redirect(url_for("exam_dashboard",exam_id=exam_id))
    random.shuffle(qs)
    return render_template("quiz.html",questions=qs,mode=mode,exam_id=exam_id)



@app.route("/session",methods=["POST"])
def session():
    data=request.get_json(); c=conn()
    c.execute("""INSERT INTO sessions(exam_id,mode,total,correct,duration,created_at)
                 VALUES(?,?,?,?,?,?)""",(data.get("exam_id"),data.get("mode",""),
                 int(data["total"]),int(data["correct"]),float(data["duration"]),datetime.now().isoformat()))
    c.commit(); c.close(); return jsonify({"ok":True})

if __name__=="__main__":
    init_db()
    app.run(host="127.0.0.1",port=5050,debug=False)
