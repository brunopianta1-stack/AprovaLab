from datetime import datetime, timedelta

TARGET_MASTERY = 0.90
RATINGS = {"again", "hard", "good", "easy"}


def question_stats(c, qid):
    row = c.execute("SELECT COUNT(*) attempts, COALESCE(SUM(correct),0) corrects FROM reviews WHERE question_id=?", (qid,)).fetchone()
    attempts, corrects = int(row["attempts"] or 0), int(row["corrects"] or 0)
    return attempts, corrects, (corrects / attempts if attempts else 0.0)


def human_due(due_at):
    if not due_at:
        return "Revisar agora"
    try:
        seconds = (datetime.fromisoformat(due_at) - datetime.now()).total_seconds()
    except Exception:
        return "Revisão programada"
    if seconds <= 0: return "Revisar agora"
    if seconds < 3600: return f"daqui a {max(1, round(seconds / 60))} min"
    if seconds < 86400: return f"daqui a {max(1, round(seconds / 3600))} h"
    days = max(1, round(seconds / 86400))
    if days == 1: return "amanhã"
    if days < 30: return f"daqui a {days} dias"
    if days < 365: return f"daqui a {max(1, round(days / 30))} meses"
    return f"daqui a {max(1, round(days / 365))} anos"


def memory_snapshot(c, qid):
    attempts, corrects, accuracy = question_stats(c, qid)
    s = c.execute("SELECT * FROM schedule WHERE question_id=?", (qid,)).fetchone()
    state = (s["state"] if s and "state" in s.keys() else None) or ("new" if attempts == 0 else "review")
    interval = int(s["interval_days"] or 0) if s else 0
    suspended = bool(s["suspended"] if s and "suspended" in s.keys() else 0)
    if suspended: label, css = "Sanguessuga suspensa", "leech"
    elif state == "new": label, css = "Nova", "new"
    elif state in ("learning", "relearning"): label, css = "Aprendendo", "learning"
    elif interval < 21: label, css = "Jovem", "consolidating"
    else: label, css = "Madura", "mastered90"
    return {"attempts": attempts, "corrects": corrects, "accuracy": round(accuracy * 100, 1),
            "memory_label": label, "memory_class": css, "interval_days": interval,
            "ease": round(float(s["ease"] or 2.5), 2) if s else 2.5,
            "repetitions": int(s["repetitions"] or 0) if s else 0,
            "lapses": int(s["lapses"] or 0) if s else 0, "due_at": s["due_at"] if s else None,
            "due_label": human_due(s["due_at"] if s else None), "state": state,
            "suspended": suspended, "mastered": state == "review" and interval >= 21}


def next_review(c, qid, rating, leech_threshold=8):
    """Agendador adaptativo inspirado no FSRS, com aprendizado curto e estabilidade individual."""
    rating = rating.lower()
    if rating not in RATINGS: raise ValueError("Avaliação inválida.")
    now = datetime.now()
    s = c.execute("SELECT * FROM schedule WHERE question_id=?", (qid,)).fetchone()
    state = (s["state"] if s and "state" in s.keys() else None) or "new"
    interval = int(s["interval_days"] or 0) if s else 0
    ease = float(s["ease"] or 2.5) if s else 2.5
    reps = int(s["repetitions"] or 0) if s else 0
    lapses = int(s["lapses"] or 0) if s else 0
    stability = float(s["stability"] or 0.4) if s and "stability" in s.keys() else 0.4
    difficulty = float(s["difficulty_score"] or 5.0) if s and "difficulty_score" in s.keys() else 5.0
    if rating == "again":
        lapses += 1; state = "relearning" if reps else "learning"
        stability = max(.15, stability * .45); difficulty = min(10., difficulty + .8)
        due, interval = now + timedelta(minutes=1), 0
    elif state in ("new", "learning", "relearning"):
        difficulty = max(1., difficulty + {"hard": .3, "good": -.2, "easy": -.6}[rating])
        if rating == "hard": state, due, interval = "learning", now + timedelta(minutes=6), 0
        elif rating == "good" and reps == 0:
            reps += 1; state, due, interval = "learning", now + timedelta(minutes=10), 0
        else:
            reps += 1; state = "review"; interval = 4 if rating == "easy" else 1
            stability = float(interval); due = now + timedelta(days=interval)
    else:
        reps += 1
        difficulty = min(10., max(1., difficulty + {"hard": .35, "good": -.12, "easy": -.45}[rating]))
        growth = {"hard": 1.25, "good": 1.9, "easy": 2.7}[rating] * max(.75, 1.15 - difficulty / 20)
        stability = min(3650., max(stability + .5, stability * growth))
        interval = max(1, min(3650, round(stability)))
        ease = min(3.4, max(1.3, ease + {"hard": -.12, "good": .02, "easy": .1}[rating]))
        due = now + timedelta(days=interval)
    suspended = int(lapses >= max(1, int(leech_threshold)))
    c.execute("""INSERT INTO schedule(question_id,interval_days,ease,repetitions,lapses,due_at,state,stability,difficulty_score,suspended)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(question_id) DO UPDATE SET interval_days=excluded.interval_days,
        ease=excluded.ease,repetitions=excluded.repetitions,lapses=excluded.lapses,due_at=excluded.due_at,
        state=excluded.state,stability=excluded.stability,difficulty_score=excluded.difficulty_score,suspended=excluded.suspended""",
        (qid, interval, ease, reps, lapses, due.isoformat(), state, stability, difficulty, suspended))
    return memory_snapshot(c, qid)
