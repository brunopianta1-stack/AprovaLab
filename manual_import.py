import csv
import io
import json
import re
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from openpyxl import load_workbook

manual_import_bp = Blueprint(
    "manual_import",
    __name__
)


def normalize_answer(value):
    if value is None:
        return None

    text = str(value).strip().lower()

    if text in ("certo", "c", "true", "verdadeiro", "1"):
        return "Certo"

    if text in ("errado", "e", "false", "falso", "0"):
        return "Errado"

    return None


def normalize_question(raw):
    aliases = {
        "statement": [
            "statement",
            "afirmacao",
            "afirmação",
            "questao",
            "questão",
            "enunciado"
        ],
        "answer": [
            "answer",
            "gabarito",
            "resposta"
        ],
        "explanation": [
            "explanation",
            "explicacao",
            "explicação",
            "comentario",
            "comentário"
        ],
        "basis": [
            "basis",
            "fundamento",
            "base",
            "fonte"
        ],
        "page": [
            "page",
            "pagina",
            "página"
        ],
        "topic": [
            "topic",
            "topico",
            "tópico",
            "assunto"
        ],
        "difficulty": [
            "difficulty",
            "dificuldade",
            "nivel",
            "nível"
        ]
    }

    normalized_raw = {
        str(k).strip().lower(): v
        for k, v in raw.items()
        if k is not None
    }

    result = {}

    for field, names in aliases.items():
        value = None

        for name in names:
            if name in normalized_raw:
                value = normalized_raw[name]
                break

        result[field] = value

    statement = str(
        result.get("statement") or ""
    ).strip()

    answer = normalize_answer(
        result.get("answer")
    )

    explanation = str(
        result.get("explanation") or ""
    ).strip()

    basis = str(
        result.get("basis") or ""
    ).strip()

    topic = str(
        result.get("topic") or ""
    ).strip()

    difficulty = str(
        result.get("difficulty") or ""
    ).strip()

    page = result.get("page")

    try:
        page = int(float(page)) if page not in (None, "") else None
    except Exception:
        page = None

    if not statement:
        raise ValueError(
            "Questão sem enunciado."
        )

    if not answer:
        raise ValueError(
            f"Gabarito inválido na questão: {statement[:60]}"
        )

    if not explanation:
        explanation = "Explicação não informada."

    if not basis:
        basis = "Fundamento não informado."

    return {
        "statement": statement,
        "answer": answer,
        "explanation": explanation,
        "basis": basis,
        "page": page,
        "topic": topic,
        "difficulty": difficulty
    }


def parse_json(content):
    data = json.loads(content)

    if isinstance(data, dict):
        data = data.get("questions", [])

    if not isinstance(data, list):
        raise ValueError(
            "O JSON deve conter uma lista de questões."
        )

    return [
        normalize_question(q)
        for q in data
    ]


def parse_csv(content_bytes):
    text = None

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = content_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        raise ValueError(
            "Não foi possível ler o arquivo CSV."
        )

    sample = text[:4096]

    try:
        dialect = csv.Sniffer().sniff(
            sample,
            delimiters=",;\t"
        )
    except Exception:
        dialect = csv.excel
        dialect.delimiter = ";"

    reader = csv.DictReader(
        io.StringIO(text),
        dialect=dialect
    )

    return [
        normalize_question(row)
        for row in reader
    ]


def parse_xlsx(file_stream):
    workbook = load_workbook(
        file_stream,
        read_only=True,
        data_only=True
    )

    sheet = workbook.active

    rows = list(
        sheet.iter_rows(values_only=True)
    )

    if not rows:
        return []

    headers = [
        str(v).strip()
        if v is not None
        else ""
        for v in rows[0]
    ]

    questions = []

    for values in rows[1:]:

        if not any(
            value not in (None, "")
            for value in values
        ):
            continue

        raw = dict(
            zip(headers, values)
        )

        questions.append(
            normalize_question(raw)
        )

    return questions


def parse_manual_text(text):
    blocks = re.split(
        r"\n\s*---+\s*\n",
        text.strip()
    )

    questions = []

    patterns = {
        "statement": r"(?im)^(?:afirmação|afirmacao|questão|questao|enunciado)\s*:\s*(.+)",
        "answer": r"(?im)^(?:gabarito|resposta)\s*:\s*(.+)",
        "explanation": r"(?ims)^(?:explicação|explicacao|comentário|comentario)\s*:\s*(.+?)(?=^\s*(?:fundamento|base|fonte|página|pagina|tópico|topico|dificuldade)\s*:|\Z)",
        "basis": r"(?ims)^(?:fundamento|base|fonte)\s*:\s*(.+?)(?=^\s*(?:página|pagina|tópico|topico|dificuldade)\s*:|\Z)",
        "page": r"(?im)^(?:página|pagina)\s*:\s*(.+)",
        "topic": r"(?im)^(?:tópico|topico|assunto)\s*:\s*(.+)",
        "difficulty": r"(?im)^(?:dificuldade|nível|nivel)\s*:\s*(.+)"
    }

    for block in blocks:

        if not block.strip():
            continue

        raw = {}

        for field, pattern in patterns.items():

            match = re.search(
                pattern,
                block
            )

            raw[field] = (
                match.group(1).strip()
                if match
                else ""
            )

        questions.append(
            normalize_question(raw)
        )

    return questions


def save_questions(
    conn_func,
    subject_id,
    questions
):
    c = conn_func()

    ids = []
    imported = 0
    duplicates = 0

    for q in questions:

        existing = c.execute(
            """
            SELECT id
            FROM questions
            WHERE subject_id=?
              AND statement=?
            """,
            (
                subject_id,
                q["statement"]
            )
        ).fetchone()

        if existing:
            duplicates += 1
            ids.append(existing["id"])
            continue

        cur = c.execute(
            """
            INSERT INTO questions(
                doc_id,
                subject_id,
                notebook_id,
                statement,
                answer,
                explanation,
                basis,
                page,
                topic,
                difficulty,
                created_at
            )
            VALUES(
                NULL,
                ?,
                NULL,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?
            )
            """,
            (
                subject_id,
                q["statement"],
                q["answer"],
                q["explanation"],
                q["basis"],
                q["page"],
                q["topic"],
                q["difficulty"],
                datetime.now().isoformat()
            )
        )

        qid = cur.lastrowid

        ids.append(qid)
        imported += 1

        c.execute(
            """
            INSERT OR IGNORE INTO schedule(
                question_id,
                due_at
            )
            VALUES(?,?)
            """,
            (
                qid,
                datetime.now().isoformat()
            )
        )

    c.commit()
    c.close()

    return ids, imported, duplicates


@manual_import_bp.route(
    "/exam/<int:exam_id>/import/<int:subject_id>",
    methods=["GET", "POST"]
)
def import_subject(
    exam_id,
    subject_id
):
    from app import conn

    c = conn()

    exam = c.execute(
        """
        SELECT *
        FROM exams
        WHERE id=?
        """,
        (exam_id,)
    ).fetchone()

    subject = c.execute(
        """
        SELECT *
        FROM subjects
        WHERE id=?
          AND exam_id=?
        """,
        (
            subject_id,
            exam_id
        )
    ).fetchone()

    c.close()

    if not exam or not subject:
        flash(
            "Disciplina não encontrada.",
            "error"
        )

        return redirect(
            url_for(
                "exam_dashboard",
                exam_id=exam_id
            )
        )

    if request.method == "POST":

        try:
            questions = []

            manual_text = request.form.get(
                "manual_text",
                ""
            ).strip()

            upload = request.files.get(
                "question_file"
            )

            if manual_text:
                questions = parse_manual_text(
                    manual_text
                )

            elif upload and upload.filename:

                filename = upload.filename.lower()

                if filename.endswith(".json"):

                    content = upload.read().decode(
                        "utf-8-sig"
                    )

                    questions = parse_json(
                        content
                    )

                elif filename.endswith(".csv"):

                    questions = parse_csv(
                        upload.read()
                    )

                elif filename.endswith(".xlsx"):

                    questions = parse_xlsx(
                        upload
                    )

                else:
                    raise ValueError(
                        "Formato não suportado. Use JSON, CSV ou XLSX."
                    )

            else:
                raise ValueError(
                    "Cole questões ou selecione um arquivo."
                )

            if not questions:
                raise ValueError(
                    "Nenhuma questão válida foi encontrada."
                )

            ids, imported, duplicates = save_questions(
                conn,
                subject_id,
                questions
            )

            flash(
                f"{imported} questões importadas. "
                f"{duplicates} duplicadas ignoradas.",
                "ok"
            )

            if ids:
                return redirect(
                    url_for(
                        "quiz",
                        exam_id=exam_id,
                        mode="ids",
                        ids=",".join(
                            map(str, ids)
                        )
                    )
                )

        except Exception as e:

            flash(
                f"Erro na importação: {e}",
                "error"
            )

    return render_template(
        "import_subject.html",
        exam=exam,
        subject=subject
    )