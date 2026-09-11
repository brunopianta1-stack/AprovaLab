from datetime import datetime, timedelta

TARGET_MASTERY = 0.90


def question_stats(c, qid):
    """
    Retorna o histórico acumulado da questão.
    """
    row = c.execute("""
        SELECT
            COUNT(*) AS attempts,
            COALESCE(SUM(correct), 0) AS corrects
        FROM reviews
        WHERE question_id=?
    """, (qid,)).fetchone()

    attempts = int(row["attempts"] or 0)
    corrects = int(row["corrects"] or 0)

    accuracy = (corrects / attempts) if attempts else 0.0

    return attempts, corrects, accuracy


def memory_level(attempts, accuracy):
    """
    Classificação visual do estágio de memorização.
    """

    if attempts == 0:
        return "Nova", "new"

    if attempts < 3 or accuracy < 0.70:
        return "Aprendendo", "learning"

    if attempts < 5 or accuracy < 0.90:
        return "Consolidando", "consolidating"

    if accuracy < 0.95:
        return "Dominada 90%", "mastered90"

    return "Dominada 95%", "mastered95"


def human_due(due_at):
    """
    Converte a próxima revisão para texto amigável.
    """

    if not due_at:
        return "Revisar agora"

    try:
        due = datetime.fromisoformat(due_at)
    except Exception:
        return "Revisão programada"

    now = datetime.now()
    diff = due - now

    days = diff.days

    if diff.total_seconds() <= 0:
        return "Revisar agora"

    if days <= 0:
        return "amanhã"

    if days == 1:
        return "amanhã"

    if days < 30:
        return f"daqui a {days} dias"

    months = round(days / 30)

    if months <= 1:
        return "daqui a 1 mês"

    if days < 365:
        return f"daqui a {months} meses"

    years = round(days / 365)

    if years <= 1:
        return "daqui a 1 ano"

    return f"daqui a {years} anos"


def memory_snapshot(c, qid):
    """
    Retorna o estado atual da memória da questão.
    """

    attempts, corrects, accuracy = question_stats(c, qid)

    s = c.execute("""
        SELECT *
        FROM schedule
        WHERE question_id=?
    """, (qid,)).fetchone()

    interval = int(s["interval_days"] or 0) if s else 0
    ease = float(s["ease"] or 2.5) if s else 2.5
    reps = int(s["repetitions"] or 0) if s else 0
    lapses = int(s["lapses"] or 0) if s else 0
    due_at = s["due_at"] if s else None

    label, css_class = memory_level(attempts, accuracy)

    return {
        "attempts": attempts,
        "corrects": corrects,
        "accuracy": round(accuracy * 100, 1),
        "memory_label": label,
        "memory_class": css_class,
        "interval_days": interval,
        "ease": round(ease, 2),
        "repetitions": reps,
        "lapses": lapses,
        "due_at": due_at,
        "due_label": human_due(due_at),
        "mastered": attempts >= 5 and accuracy >= TARGET_MASTERY,
    }


def next_review(c, qid, correct, confidence):
    """
    Calcula a próxima revisão da questão.

    Lógica:
    - erro -> intervalo muito curto;
    - acertos consecutivos -> intervalo crescente;
    - confiança influencia o intervalo;
    - >= 90% de acerto após pelo menos 5 tentativas -> domínio;
    - >= 95% após pelo menos 8 tentativas -> domínio avançado;
    - erros posteriores reduzem o intervalo imediatamente.
    """

    now = datetime.now()

    s = c.execute("""
        SELECT *
        FROM schedule
        WHERE question_id=?
    """, (qid,)).fetchone()

    interval = int(s["interval_days"] or 0) if s else 0
    ease = float(s["ease"] or 2.5) if s else 2.5
    reps = int(s["repetitions"] or 0) if s else 0
    lapses = int(s["lapses"] or 0) if s else 0

    attempts, corrects, accuracy = question_stats(c, qid)

    if not correct:

        lapses += 1
        reps = 0

        ease = max(1.30, ease - 0.20)

        # Questões já relativamente conhecidas não voltam
        # necessariamente para revisão imediata.
        if attempts >= 5 and accuracy >= 0.75:
            interval = 2
        else:
            interval = 1

    else:

        reps += 1

        # Primeira fase de aprendizagem.
        if reps == 1:
            interval = 1

        elif reps == 2:
            interval = 3

        elif reps == 3:
            interval = 7

        else:
            interval = max(
                8,
                round(max(interval, 1) * ease)
            )

        # Confiança declarada pelo aluno.
        confidence_factor = {
            "Certeza": 1.15,
            "Dúvida": 0.90,
            "Chute": 0.65,
        }.get(confidence, 0.90)

        interval = max(
            1,
            round(interval * confidence_factor)
        )

        # Domínio de 90%.
        if attempts >= 5 and accuracy >= 0.90:

            interval = max(interval, 14)

            interval = round(
                interval * 1.80
            )

            ease = min(
                3.20,
                ease + 0.08
            )

        # Domínio avançado de 95%.
        if attempts >= 8 and accuracy >= 0.95:

            interval = max(interval, 30)

            interval = round(
                interval * 1.35
            )

            ease = min(
                3.40,
                ease + 0.05
            )

        # Limite de segurança.
        interval = min(interval, 365)

    due = (
        now + timedelta(days=interval)
    ).isoformat()

    c.execute("""
        INSERT INTO schedule(
            question_id,
            interval_days,
            ease,
            repetitions,
            lapses,
            due_at
        )
        VALUES(?,?,?,?,?,?)

        ON CONFLICT(question_id)
        DO UPDATE SET

            interval_days=excluded.interval_days,
            ease=excluded.ease,
            repetitions=excluded.repetitions,
            lapses=excluded.lapses,
            due_at=excluded.due_at
    """, (
        qid,
        interval,
        ease,
        reps,
        lapses,
        due
    ))

    return memory_snapshot(c, qid)
