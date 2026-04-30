import csv
import io
import json
import os
import re
import socket
import sqlite3
import subprocess
from datetime import datetime
from functools import wraps
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    Response,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import openai as openai_legacy
except ImportError:
    openai_legacy = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import ollama
except ImportError:
    ollama = None

try:
    from fpdf import FPDF
except ImportError:
    FPDF = None

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, "progress.db")
CONTENT_DIR = os.path.join(BASE_DIR, "content")
LESSONS_FILE = os.path.join(CONTENT_DIR, "lessons.json")
QUIZZES_FILE = os.path.join(CONTENT_DIR, "quizzes.json")
PDF_DIR = os.path.join(BASE_DIR, "static", "pdfs")
CONTENT_IMAGES_DIR = os.path.join(CONTENT_DIR, "images")

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")

if load_dotenv is not None:
    # Load .env values when available; does nothing if no .env file exists.
    load_dotenv()

@app.route("/content/images/<path:filename>")
def content_image(filename):
    return send_from_directory(CONTENT_IMAGES_DIR, filename)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-3.5-turbo")
OPENAI_ENHANCED_MODEL = os.environ.get("OPENAI_ENHANCED_MODEL", OPENAI_MODEL)
OPENAI_API_KEY = OPENAI_API_KEY.strip() if OPENAI_API_KEY else ""
OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY) if OpenAI is not None and OPENAI_API_KEY else None
OPENAI_AVAILABLE = bool(OPENAI_API_KEY) and (OPENAI_CLIENT is not None or openai_legacy is not None)
if openai_legacy is not None and OPENAI_API_KEY:
    openai_legacy.api_key = OPENAI_API_KEY

OLLAMA_AVAILABLE = ollama is not None
OFFLINE_MODEL = os.environ.get("OFFLINE_MODEL", "qwen2.5:3b-instruct")
OFFLINE_MODEL_PINNED = os.environ.get("OFFLINE_MODEL_PINNED", "0").strip().lower() in ("1", "true", "yes", "on")
OFFLINE_MODEL_AUTO_PULL = os.environ.get("OFFLINE_MODEL_AUTO_PULL", "1").strip().lower() in ("1", "true", "yes", "on")
OLLAMA_AUTO_START = os.environ.get("OLLAMA_AUTO_START", "1").strip().lower() in ("1", "true", "yes", "on")

SUBJECTS = ["Physics", "Biology", "Chemistry", "English", "Mathematics", "Programming"]
GRADES = ["Grade 9", "Grade 10", "Grade 11", "Grade 12"]


def ensure_ollama_service_running():
    if not OLLAMA_AVAILABLE or not OLLAMA_AUTO_START:
        return False

    try:
        ollama.list()
        return True
    except Exception:
        pass

    ollama_paths = [
        r"C:\Program Files\Ollama\ollama.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
    ]
    for path in ollama_paths:
        if not path or not os.path.exists(path):
            continue
        try:
            subprocess.Popen([path, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


def is_online():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except OSError:
        return False


def get_db():
    db = getattr(g, "db", None)
    if db is None:
        db = g.db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(exception=None):
    db = getattr(g, "db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'student',
            subject TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            lesson_id TEXT NOT NULL,
            completed INTEGER DEFAULT 0,
            score INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            last_updated TEXT,
            UNIQUE(user_id, lesson_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS quiz_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            lesson_id TEXT NOT NULL,
            answers_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, lesson_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    db.commit()

    cursor = db.execute("PRAGMA table_info(users)")
    user_columns = [row[1] for row in cursor.fetchall()]
    if "role" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'student'")
        db.commit()
    if "subject" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN subject TEXT")
        db.commit()
    if "grade" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN grade TEXT")
        db.commit()

    cursor = db.execute("PRAGMA table_info(progress)")
    columns = [row[1] for row in cursor.fetchall()]
    if "user_id" not in columns:
        db.execute("ALTER TABLE progress ADD COLUMN user_id INTEGER DEFAULT 0")
        db.commit()


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def get_current_user():
    user_id = session.get("user_id")
    if user_id is None:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


@app.context_processor
def inject_user():
    user = get_current_user()
    return {
        "current_user": user,
        "is_authenticated": bool(user),
    }


def create_user(username, password, role="student", subject=None, grade=None):
    db = get_db()
    now = datetime.utcnow().isoformat()
    password_hash = generate_password_hash(password)
    db.execute(
        "INSERT INTO users (username, password_hash, role, subject, grade, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (username, password_hash, role, subject, grade, now),
    )
    db.commit()


def authenticate_user(username, password):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


def save_lessons():
    with open(LESSONS_FILE, "w", encoding="utf-8") as fh:
        json.dump(LESSONS, fh, indent=2, ensure_ascii=False)


def reload_content():
    global LESSONS, QUIZZES, LESSON_INDEX, QUIZ_INDEX
    LESSONS = load_json(LESSONS_FILE)
    QUIZZES = load_json(QUIZZES_FILE)
    LESSON_INDEX = {lesson["id"]: lesson for lesson in LESSONS}
    QUIZ_INDEX = {item["lesson_id"]: item for item in QUIZZES}


def get_content_signature():
    return (
        os.path.getmtime(LESSONS_FILE),
        os.path.getmtime(QUIZZES_FILE),
    )


def maybe_reload_content():
    global CONTENT_SIGNATURE
    current_signature = get_content_signature()
    if current_signature != CONTENT_SIGNATURE:
        reload_content()
        initialize_pdfs()
        CONTENT_SIGNATURE = current_signature


LESSONS = load_json(LESSONS_FILE)
QUIZZES = load_json(QUIZZES_FILE)
LESSON_INDEX = {lesson["id"]: lesson for lesson in LESSONS}
QUIZ_INDEX = {item["lesson_id"]: item for item in QUIZZES}
CONTENT_SIGNATURE = get_content_signature()


def get_progress(user_id=None):
    if user_id is None:
        return {}
    db = get_db()
    rows = db.execute("SELECT * FROM progress WHERE user_id = ?", (user_id,)).fetchall()
    data = {row["lesson_id"]: dict(row) for row in rows}
    return data


def update_progress(lesson_id, user_id, completed=None, score=None, attempts=None):
    db = get_db()
    now = datetime.utcnow().isoformat()
    existing = db.execute(
        "SELECT * FROM progress WHERE user_id = ? AND lesson_id = ?",
        (user_id, lesson_id),
    ).fetchone()
    if existing is None:
        completed_val = 1 if completed else 0
        score_val = score if score is not None else 0
        attempts_val = attempts if attempts is not None else 0
        db.execute(
            "INSERT INTO progress (user_id, lesson_id, completed, score, attempts, last_updated) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, lesson_id, completed_val, score_val, attempts_val, now),
        )
    else:
        completed_val = existing["completed"] if completed is None else (1 if completed else 0)
        score_val = existing["score"] if score is None else score
        attempts_val = existing["attempts"] if attempts is None else attempts
        db.execute(
            "UPDATE progress SET completed = ?, score = ?, attempts = ?, last_updated = ? WHERE user_id = ? AND lesson_id = ?",
            (completed_val, score_val, attempts_val, now, user_id, lesson_id),
        )
    db.commit()


def get_quiz_draft(user_id, lesson_id):
    db = get_db()
    row = db.execute(
        "SELECT answers_json FROM quiz_drafts WHERE user_id = ? AND lesson_id = ?",
        (user_id, lesson_id),
    ).fetchone()
    if row is None:
        return {}
    try:
        parsed = json.loads(row["answers_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save_quiz_draft(user_id, lesson_id, answers):
    db = get_db()
    now = datetime.utcnow().isoformat()
    payload = json.dumps(answers, ensure_ascii=False)
    db.execute(
        """
        INSERT INTO quiz_drafts (user_id, lesson_id, answers_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, lesson_id)
        DO UPDATE SET answers_json = excluded.answers_json, updated_at = excluded.updated_at
        """,
        (user_id, lesson_id, payload, now),
    )
    db.commit()


def clear_quiz_draft(user_id, lesson_id):
    db = get_db()
    db.execute(
        "DELETE FROM quiz_drafts WHERE user_id = ? AND lesson_id = ?",
        (user_id, lesson_id),
    )
    db.commit()


def get_suggestion(progress_data, grade=None):
    lesson_pool = [lesson for lesson in LESSONS if grade is None or lesson.get("grade") == grade]
    if not lesson_pool:
        lesson_pool = LESSONS
    incomplete = [lesson for lesson in lesson_pool if lesson["id"] not in progress_data or progress_data[lesson["id"]]["completed"] == 0]
    if incomplete:
        return incomplete[0]
    low_scores = [lesson for lesson in lesson_pool if lesson["id"] in progress_data and progress_data[lesson["id"]]["score"] < 70]
    if low_scores:
        return sorted(low_scores, key=lambda l: progress_data[l["id"]]["score"])[0]
    return lesson_pool[0]


def get_ai_system_message():
    return {
        "role": "system",
        "content": (
            "You are an elite offline high-school tutor for Tech4Edu students. "
            "You can teach science, math, language, and programming with practical clarity. "
            "Core behavior: "
            "1) Give a direct answer first (1-3 lines), then a clear step-by-step explanation. "
            "2) Adapt depth to the student and grade level; keep language simple but not shallow. "
            "3) For problem solving, show method, formula choices, units, and common mistakes. "
            "4) For coding questions, provide working examples and explain how to test/debug them. "
            "5) For study planning, return a realistic plan with timing and checkpoints. "
            "6) End with one short check question or a mini-practice prompt. "
            "7) If uncertain, say exactly what is uncertain and suggest a way to verify. "
            "8) Stay supportive and action-oriented; avoid vague motivational filler."
        ),
    }


def get_openai_chatgpt_message():
    return {
        "role": "system",
        "content": (
            "You are a helpful, accurate, and concise AI assistant. "
            "Answer clearly, ask follow-up questions when useful, and adapt depth to the user's request."
        ),
    }


def normalize_history(history):
    if not isinstance(history, list):
        return []
    cleaned = []
    for item in history[-20:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            cleaned.append({"role": role, "content": content.strip()})
    return cleaned


AI_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "from", "into", "about", "what", "when", "where", "which",
    "have", "has", "had", "you", "your", "they", "them", "their", "then", "than", "just", "like", "want",
    "need", "please", "help", "tell", "show", "does", "did", "how", "why", "can", "could", "would", "should",
    "are", "was", "were", "will", "shall", "may", "might", "not", "all", "any", "more", "some", "very",
    "also", "only", "over", "under", "between", "because", "through", "using", "use", "make", "makes", "made",
    "law", "laws", "question", "questions", "step", "steps", "solve", "solving", "explain", "explanation",
    "example", "examples", "give", "need", "know",
}


def extract_keywords(text, limit=12):
    if not isinstance(text, str):
        return []
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{2,}", text.lower())
    seen = set()
    keywords = []
    for word in words:
        if word in AI_STOPWORDS:
            continue
        if word in seen:
            continue
        seen.add(word)
        keywords.append(word)
        if len(keywords) >= limit:
            break
    return keywords


def score_lesson_relevance(lesson, keywords):
    if not keywords:
        return 0
    fields = [lesson.get("title", ""), lesson.get("summary", ""), lesson.get("subject", "")]
    for section in lesson.get("sections", []):
        if not isinstance(section, dict):
            continue
        fields.append(section.get("heading", ""))
        fields.append(section.get("body", ""))
    blob = " ".join(part for part in fields if isinstance(part, str)).lower()
    score = 0
    distinct_hits = 0
    for keyword in keywords:
        occurrences = blob.count(keyword)
        if occurrences:
            distinct_hits += 1
            score += min(occurrences, 4)
    if distinct_hits < 1:
        return 0
    return score


def infer_subject_from_question(question_text):
    q = (question_text or "").lower()
    subject_patterns = [
        ("Physics", r"electric|circuit|voltage|current|resistance|force|motion|gravity|energy|heat|wave|optics|mechanic"),
        ("Chemistry", r"atom|molecule|reaction|periodic|acid|base|salt|bond|chemical|stoichi"),
        ("Biology", r"cell|ecosystem|organism|photosynthesis|genetics|enzyme|respiration|biology"),
        ("Mathematics", r"algebra|equation|geometry|function|graph|calculus|probability|statistics|triangle"),
        ("Programming", r"programming|python|code|algorithm|loop|function|debug|variable|class"),
        ("English", r"grammar|essay|paragraph|comprehension|literature|poem|english|writing"),
    ]
    for subject, pattern in subject_patterns:
        if re.search(pattern, q):
            return subject
    return None


def get_relevant_lessons(question_text, grade=None, limit=3):
    keywords = extract_keywords(question_text)
    if not keywords:
        return []

    scored = []
    for lesson in LESSONS:
        score = score_lesson_relevance(lesson, keywords)
        if score <= 0:
            continue
        lesson_grade = lesson.get("grade")
        if grade and lesson_grade == grade:
            score += 2
        scored.append((score, lesson))

    scored.sort(key=lambda item: item[0], reverse=True)
    top_lessons = []

    candidate_lessons = [lesson for _score, lesson in scored]
    if not candidate_lessons:
        q = (question_text or "").lower()
        broad_subject_request = bool(re.search(r"\b(physics|chemistry|biology|mathematics|math|programming|english)\b", q))
        inferred_subject = infer_subject_from_question(question_text)
        if inferred_subject and broad_subject_request and len(keywords) <= 5:
            candidate_lessons = [
                lesson
                for lesson in LESSONS
                if lesson.get("subject") == inferred_subject and (grade is None or lesson.get("grade") == grade)
            ]
            if not candidate_lessons:
                candidate_lessons = [lesson for lesson in LESSONS if lesson.get("subject") == inferred_subject]

    for lesson in candidate_lessons[:limit]:
        section_titles = []
        for section in lesson.get("sections", []):
            heading = section.get("heading") if isinstance(section, dict) else None
            if isinstance(heading, str) and heading.strip():
                section_titles.append(heading.strip())
            if len(section_titles) >= 2:
                break
        top_lessons.append(
            {
                "title": lesson.get("title", "Untitled lesson"),
                "subject": lesson.get("subject", "General"),
                "grade": lesson.get("grade") or "Unknown grade",
                "summary": lesson.get("summary", "")[:220],
                "section_titles": section_titles,
                "sections": lesson.get("sections", []),
            }
        )
    return top_lessons


def build_learning_context(question_text, user=None):
    user_grade = None
    if user and isinstance(user, dict):
        user_grade = user.get("grade")
    matches = get_relevant_lessons(question_text, grade=user_grade, limit=3)
    if not matches:
        return ""

    lines = ["Local curriculum context (use when relevant):"]
    for item in matches:
        section_hint = ""
        if item["section_titles"]:
            section_hint = f" | sections: {', '.join(item['section_titles'])}"
        lines.append(
            f"- {item['title']} [{item['subject']}, {item['grade']}]"
            f" | summary: {item['summary']}{section_hint}"
        )
    return "\n".join(lines)


def build_local_tutor_fallback(question_text, user=None):
    grade = user.get("grade") if isinstance(user, dict) else None
    matches = get_relevant_lessons(question_text, grade=grade, limit=1)
    if not matches:
        return ""

    lesson = matches[0]
    steps = []
    for section in lesson.get("sections", []):
        if not isinstance(section, dict):
            continue
        heading = (section.get("heading") or "").strip()
        body = re.sub(r"\s+", " ", (section.get("body") or "").strip())
        if heading and body:
            steps.append((heading, body[:170]))
        if len(steps) >= 3:
            break

    lines = [
        f"Quick answer: {lesson['summary']}",
        "",
        f"From your local lesson '{lesson['title']}' ({lesson['subject']}, {lesson['grade']}), try this:",
    ]
    if steps:
        for idx, (heading, body) in enumerate(steps, start=1):
            lines.append(f"{idx}. {heading}: {body}")
    else:
        lines.append("1. Read the lesson summary carefully.")
        lines.append("2. Identify key terms and formulas.")
        lines.append("3. Solve one practice question and check your method.")

    lines.append("")
    lines.append("Quick check: What is one rule/formula from this topic, and when do you apply it?")
    return "\n".join(lines)


def build_conversation_messages(question_text, history=None):
    messages = [get_ai_system_message()]
    for msg in normalize_history(history or []):
        messages.append(msg)
    messages.append({"role": "user", "content": question_text})
    return messages


def build_offline_messages(question_text, history=None, user=None):
    messages = [get_ai_system_message()]
    if user and isinstance(user, dict):
        username = user.get("username", "Student")
        grade = user.get("grade", "Unknown grade")
        messages.append(
            {
                "role": "system",
                "content": f"Student profile: name={username}, grade={grade}. Keep answers appropriate to this grade.",
            }
        )
    learning_context = build_learning_context(question_text, user=user)
    if learning_context:
        messages.append({"role": "system", "content": learning_context})
    for msg in normalize_history(history or []):
        messages.append(msg)
    messages.append({"role": "user", "content": question_text})
    return messages


def build_openai_enhanced_messages(question_text, history=None):
    messages = [get_openai_chatgpt_message()]
    for msg in normalize_history(history or []):
        messages.append(msg)
    messages.append({"role": "user", "content": question_text})
    return messages


def call_ollama(messages, model=None, temperature=0.2, max_tokens=800):
    if model is None:
        model = OFFLINE_MODEL
    try:
        response = ollama.chat(
            model=model,
            messages=messages,
            options={
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": 8192,
                "top_p": 0.9,
                "repeat_penalty": 1.08,
            },
        )
        return response["message"]["content"].strip()
    except TypeError:
        try:
            response = ollama.chat(model=model, messages=messages)
            return response["message"]["content"].strip()
        except Exception:
            return None
    except Exception:
        return None


def get_offline_model_candidates(limit=4):
    # Prefer stronger local models when installed. OFFLINE_MODEL can be pinned via OFFLINE_MODEL_PINNED=1.
    default_candidates = [OFFLINE_MODEL]
    ensure_ollama_service_running()
    try:
        listed = ollama.list()
    except Exception:
        if OFFLINE_MODEL_AUTO_PULL:
            try:
                ollama.pull(OFFLINE_MODEL)
                return [OFFLINE_MODEL]
            except Exception:
                return default_candidates
        return default_candidates

    raw_models = listed.get("models", []) if isinstance(listed, dict) else []
    installed = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        name = item.get("model") or item.get("name")
        if isinstance(name, str) and name.strip():
            installed.append(name.strip())

    if not installed:
        if OFFLINE_MODEL_AUTO_PULL:
            try:
                ollama.pull(OFFLINE_MODEL)
                return [OFFLINE_MODEL]
            except Exception:
                return default_candidates
        return default_candidates

    preference_order = [
        "qwen2.5",
        "llama3.1",
        "mistral",
        "deepseek",
        "gemma2",
        "phi3",
        "llama2",
    ]

    def model_rank(model_name):
        value = model_name.lower()
        for index, family in enumerate(preference_order):
            if family in value:
                return index
        return len(preference_order)

    def quant_penalty(model_name):
        value = model_name.lower()
        if "q4_" in value or "q4-k" in value or "q4k" in value:
            return 3
        if "q5_" in value or "q5-k" in value or "q5k" in value:
            return 2
        if "q6_" in value or "q6-k" in value or "q6k" in value:
            return 1
        return 0

    def size_bonus(model_name):
        value = model_name.lower()
        if "70b" in value or "72b" in value:
            return -3
        if "32b" in value or "34b" in value:
            return -2
        if "14b" in value:
            return -1
        return 0

    prioritized = sorted(installed, key=lambda name: (model_rank(name), quant_penalty(name) + size_bonus(name), len(name)))
    result = []
    if OFFLINE_MODEL_PINNED and OFFLINE_MODEL in installed:
        result.append(OFFLINE_MODEL)
    for model_name in prioritized:
        if model_name not in result:
            result.append(model_name)
        if len(result) >= limit:
            break

    return result or default_candidates


def call_openai(messages, model=None, temperature=0.7, max_tokens=800):
    if model is None:
        model = OPENAI_MODEL

    if not OPENAI_AVAILABLE:
        return None, "OpenAI is not configured (missing API key or package)."

    # New SDK path (openai>=1.x)
    if OPENAI_CLIENT is not None:
        try:
            completion = OPENAI_CLIENT.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = ""
            if completion and completion.choices and completion.choices[0].message:
                content = (completion.choices[0].message.content or "").strip()
            if content:
                return content, None
            return None, "OpenAI returned an empty response."
        except Exception as exc:
            return None, f"OpenAI request failed: {exc}"

    # Legacy SDK path (openai<1.x)
    if openai_legacy is not None:
        try:
            completion = openai_legacy.ChatCompletion.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = ""
            if completion and completion.choices:
                content = (completion.choices[0].message["content"] or "").strip()
            if content:
                return content, None
            return None, "OpenAI returned an empty response."
        except Exception as exc:
            return None, f"OpenAI request failed: {exc}"

    return None, "OpenAI client library not available."


def get_ai_response(question_text, history=None, enhanced=False, user=None):
    question_text = question_text.strip()
    if not question_text:
        return "Ask anything you want, and I will do my best to explain or discuss it with you."

    online = is_online()
    messages = build_offline_messages(question_text, history=history, user=user)

    if enhanced and OPENAI_AVAILABLE and online:
        content, _error = call_openai(messages, model=OPENAI_MODEL)
        if content:
            return content

    if OLLAMA_AVAILABLE:
        model_candidates = get_offline_model_candidates(limit=4)
        for model_name in model_candidates:
            response = call_ollama(messages, model=model_name, temperature=0.35, max_tokens=1200)
            if response:
                return response

    local_fallback = build_local_tutor_fallback(question_text, user=user)
    if local_fallback:
        return local_fallback

    fallback = answer_question(question_text, user=user)
    return fallback if fallback.strip() else "I couldn't generate a response right now. Please try rephrasing your question."


def answer_question(question_text, user=None):
    question = question_text.strip().lower()
    if not question:
        return "Ask anything you want, and I will do my best to explain or discuss it with you."
    grade_hint = ""
    if user and isinstance(user, dict) and user.get("grade"):
        grade_hint = f" for {user['grade']}"

    lesson_matches = get_relevant_lessons(question_text, grade=user.get("grade") if isinstance(user, dict) else None, limit=2)
    lesson_tip = ""
    if lesson_matches:
        picks = ", ".join(item["title"] for item in lesson_matches)
        lesson_tip = f"\n\nYou may want to review: {picks}."

    rules = [
        (r"^(hi|hello|hey|good morning|good evening)", "Hello! I'm here to help with lessons, study tips, or any learning questions you have."),
        (r"study|learn|exam|homework|prepare|revision", f"Try this study flow{grade_hint}: 1) 25-minute focus block, 2) 5-minute recap, 3) one practice question, 4) quick self-check. I can build a full plan for your next exam."),
        (r"ohm|ohm's law|voltage|current|resistance", "Use Ohm's law: V = I x R.\n1) List known values with units (V, A, ohms).\n2) Rearrange formula for unknown: I = V/R or R = V/I.\n3) Substitute values carefully and compute.\n4) Check if the result is realistic (higher resistance means lower current at same voltage).\nExample: if V=12V and R=4 ohms, then I=12/4=3A."),
        (r"electricity|circuit|voltage|current|resistance", "Electricity moves through a circuit when there is a voltage. Conductors like copper wire allow current to flow, while resistors slow it down."),
        (r"newton|force|motion|inertia|acceleration", "Newton's laws describe motion. Objects stay still or move steadily unless a force acts on them, and every action has an equal and opposite reaction."),
        (r"ecosystem|food chain|producers|consumers|decomposers", "An ecosystem includes living organisms and their environment. Producers make food, consumers eat producers, and decomposers break down dead material."),
        (r"heat|temperature|energy|conduction|convection|radiation", "Heat is energy transfer caused by temperature differences. It moves by conduction, convection, and radiation."),
        (r"atom|element|reaction|periodic", "Chemistry explains how atoms combine into molecules, how reactions release or absorb energy, and how the periodic table helps us organize elements."),
        (r"algebra|equation|variable|geometry|function|triangle", "Math helps you solve problems using symbols and shapes. Try breaking a question into smaller parts and solving each step on its own."),
        (r"programming|python|code|algorithm|loop|function", "Programming is writing instructions for a computer. Start with small examples, test each step, and build your idea gradually."),
        (r"discuss|talk|idea|opinion", "Let's discuss it. Tell me more about what you want to understand, and I can help guide you through the concept."),
        (r"why|how|what|when|where", "That's a great question. If you give me the exact topic or what you find confusing, I can explain it clearly and step by step."),
    ]
    for pattern, answer in rules:
        if re.search(pattern, question):
            return answer + lesson_tip
    return (
        "I can help with lessons, study tips, and learning questions. "
        "Ask me about a topic like electricity, motion, cells, equations, coding, or exam preparation. "
        "If you paste a question, I can solve it step by step."
        + lesson_tip
    )


def should_suggest_lesson(question_text):
    """Only suggest lessons for actual study intent, not greetings or small talk."""
    question = (question_text or "").strip().lower()
    if not question:
        return False

    greeting_pattern = r"^(hi|hello|hey|yo|good morning|good afternoon|good evening)\b"
    if re.search(greeting_pattern, question):
        return False

    # Require learning intent or topic terms before showing a lesson suggestion.
    study_pattern = (
        r"study|learn|lesson|topic|exam|quiz|homework|revision|"
        r"explain|teach|understand|practice|"
        r"electricity|circuit|voltage|current|resistance|"
        r"newton|force|motion|inertia|acceleration|"
        r"ecosystem|food chain|producers|consumers|decomposers|"
        r"heat|temperature|energy|conduction|convection|radiation|"
        r"atom|element|reaction|periodic|"
        r"algebra|equation|variable|geometry|function|triangle|"
        r"programming|python|code|algorithm|loop|function"
    )
    return bool(re.search(study_pattern, question))

def ensure_pdf_dir():
    os.makedirs(PDF_DIR, exist_ok=True)


def safe_pdf_text(text):
    if text is None:
        return ""
    text = str(text)
    replacements = {
        "→": "->",
        "–": "-",
        "—": "-",
        "…": "...",
        "“": '"',
        "”": '"',
        "’": "'",
        "‘": "'",
        "	": " ",
    }
    for src, target in replacements.items():
        text = text.replace(src, target)
    try:
        return text.encode("latin-1").decode("latin-1")
    except UnicodeEncodeError:
        return text.encode("latin-1", "replace").decode("latin-1")


def create_pdf_for_lesson(lesson):
    if FPDF is None:
        return

    def draw_table(pdf, headers, rows, content_width):
        if not headers:
            return

        num_cols = len(headers)
        if num_cols == 0:
            return

        col_width = content_width / num_cols
        line_height = 6

        def estimate_lines(text, width):
            # Approximate wrapping for PyFPDF table cells.
            max_chars = max(8, int(width / 2.2))
            normalized = safe_pdf_text(text).replace("\r", "")
            parts = []
            for block in normalized.split("\n"):
                if not block:
                    parts.append("")
                else:
                    parts.extend([block[i:i + max_chars] for i in range(0, len(block), max_chars)])
            return max(1, len(parts))

        def draw_header():
            pdf.set_font("Arial", "B", 11)
            for header in headers:
                pdf.cell(col_width, 8, safe_pdf_text(header), border=1, align="L")
            pdf.ln(8)

        draw_header()
        pdf.set_font("Arial", "", 10)
        for row in rows:
            cells = list(row)[:num_cols]
            while len(cells) < num_cols:
                cells.append("")

            row_height = line_height * max(estimate_lines(cell, col_width) for cell in cells)

            if pdf.get_y() + row_height > pdf.page_break_trigger:
                pdf.add_page()
                draw_header()
                pdf.set_font("Arial", "", 10)

            x_start = pdf.get_x()
            y_start = pdf.get_y()
            for idx, cell in enumerate(cells):
                x_cell = x_start + idx * col_width
                pdf.set_xy(x_cell, y_start)
                pdf.multi_cell(col_width, line_height, safe_pdf_text(cell), border=1, align="L")
                pdf.set_xy(x_cell + col_width, y_start)

            pdf.set_xy(x_start, y_start + row_height)

    ensure_pdf_dir()
    pdf_path = os.path.join(PDF_DIR, f"{lesson['id']}.pdf")
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    content_width = pdf.w - pdf.l_margin - pdf.r_margin

    pdf.set_font("Arial", "B", 18)
    pdf.cell(0, 12, safe_pdf_text(lesson["title"]), ln=True)
    pdf.set_font("Arial", "I", 12)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(content_width, 8, safe_pdf_text(f"Subject: {lesson['subject']}"))
    if lesson.get("grade"):
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(content_width, 8, safe_pdf_text(f"Grade: {lesson['grade']}"))
    pdf.ln(4)
    pdf.set_font("Arial", "", 12)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(content_width, 7, safe_pdf_text(lesson["summary"]))
    pdf.ln(4)
    for section in lesson["sections"]:
        pdf.set_font("Arial", "B", 14)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(content_width, 8, safe_pdf_text(section["heading"]))
        pdf.set_font("Arial", "", 12)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(content_width, 7, safe_pdf_text(section["body"]))
        table = section.get("table")
        if table and table.get("headers") and table.get("rows"):
            pdf.ln(1)
            pdf.set_x(pdf.l_margin)
            draw_table(pdf, table["headers"], table["rows"], content_width)
        pdf.ln(3)
    pdf.output(pdf_path)


def initialize_pdfs():
    if FPDF is None:
        return
    ensure_pdf_dir()
    for lesson in LESSONS:
        create_pdf_for_lesson(lesson)


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if get_current_user() is None:
            return redirect(url_for("login", next=request.path))
        return view(**kwargs)

    return wrapped_view


@app.before_request
def setup_app():
    maybe_reload_content()
    if not hasattr(app, "initialized"):
        init_db()
        initialize_pdfs()
        app.initialized = True


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        grade = request.form.get("grade", "")
        if not username or not password or not grade:
            error = "Username, password, and grade are required."
        elif password != confirm:
            error = "Passwords do not match."
        elif grade not in GRADES:
            error = "Please choose a valid grade."
        else:
            try:
                create_user(username, password, role="student", grade=grade)
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                error = "That username is already taken."
    return render_template("register.html", error=error, grade_options=GRADES)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = authenticate_user(username, password)
        if user is None:
            error = "Invalid username or password."
        else:
            session.clear()
            session["user_id"] = user["id"]
            next_page = request.args.get("next") or url_for("index")
            return redirect(next_page)
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/subject/<subject>")
@login_required
def subject_page(subject):
    subject = subject.capitalize()
    if subject not in SUBJECTS:
        return redirect(url_for("index"))
    user = get_current_user()
    default_grade = user["grade"] if user and user["grade"] in GRADES else GRADES[0]
    selected_grade = request.args.get("grade", default_grade)
    if selected_grade not in GRADES:
        selected_grade = default_grade
    progress_data = get_progress(user["id"])
    subject_lessons = [lesson for lesson in LESSONS if lesson["subject"] == subject and lesson.get("grade") == selected_grade]
    grouped = {}
    for lesson in sorted(subject_lessons, key=lambda l: (l.get("unit", "General"), l["title"])):
        unit_name = lesson.get("unit", "General")
        grouped.setdefault(unit_name, []).append({
            "id": lesson["id"],
            "title": lesson["title"],
            "summary": lesson["summary"],
            "completed": bool(progress_data.get(lesson["id"], {}).get("completed", 0)),
            "score": progress_data.get(lesson["id"], {}).get("score", None),
        })
    grouped_lessons = [{"unit": unit, "lessons": lessons} for unit, lessons in grouped.items()]
    return render_template("subject.html", subject=subject, grouped_lessons=grouped_lessons, selected_grade=selected_grade, grade_options=GRADES)


@app.route("/")
@login_required
def index():
    user = get_current_user()
    default_grade = user["grade"] if user and user["grade"] in GRADES else GRADES[0]
    selected_grade = request.args.get("grade", default_grade)
    if selected_grade not in GRADES:
        selected_grade = default_grade
    return render_template(
        "index.html",
        subjects=SUBJECTS,
        selected_grade=selected_grade,
        grade_options=GRADES,
    )


@app.route("/lesson/<lesson_id>")
@login_required
def lesson(lesson_id):
    lesson = LESSON_INDEX.get(lesson_id)
    if lesson is None:
        return redirect(url_for("index"))
    progress_data = get_progress(get_current_user()["id"])
    status = progress_data.get(lesson_id, {})
    return render_template("lesson.html", lesson=lesson, status=status)


@app.route("/quiz/<lesson_id>")
@login_required
def quiz(lesson_id):
    lesson = LESSON_INDEX.get(lesson_id)
    quiz = QUIZ_INDEX.get(lesson_id)
    if lesson is None or quiz is None:
        return redirect(url_for("index"))
    user = get_current_user()
    progress_data = get_progress(user["id"])
    status = progress_data.get(lesson_id, {})
    draft_answers = get_quiz_draft(user["id"], lesson_id)
    draft_saved = request.args.get("saved") == "1"
    return render_template(
        "quiz.html",
        lesson=lesson,
        quiz=quiz,
        status=status,
        draft_answers=draft_answers,
        draft_saved=draft_saved,
    )


@app.route("/submit_quiz", methods=["POST"])
@login_required
def submit_quiz():
    lesson_id = request.form.get("lesson_id")
    quiz = QUIZ_INDEX.get(lesson_id)
    if quiz is None:
        return redirect(url_for("index"))

    lesson = LESSON_INDEX.get(lesson_id)
    if lesson is None:
        return redirect(url_for("index"))

    user = get_current_user()
    action = request.form.get("action", "submit")

    answers = {}
    for question in quiz["questions"]:
        qid = str(question["id"])
        answer = request.form.get(f"question_{qid}")
        if answer is not None and answer != "":
            answers[qid] = answer

    if action == "save_exit":
        save_quiz_draft(user["id"], lesson_id, answers)
        return redirect(url_for("lesson", lesson_id=lesson_id))

    correct = 0
    total = len(quiz["questions"])
    results = []
    for question in quiz["questions"]:
        qid = str(question["id"])
        answer = answers.get(qid)
        is_correct = answer == str(question["correct_index"])
        if is_correct:
            correct += 1
        results.append(
            {
                "question": question["prompt"],
                "selected": int(answer) if answer is not None else None,
                "correct_index": question["correct_index"],
                "options": question["options"],
                "explanation": question["explanation"],
                "is_correct": is_correct,
            }
        )

    score = round((correct / total) * 100)
    current_attempts = get_progress(user["id"]).get(lesson_id, {}).get("attempts", 0)
    update_progress(lesson_id, user["id"], completed=1, score=score, attempts=current_attempts + 1)
    clear_quiz_draft(user["id"], lesson_id)
    return render_template(
        "quiz.html",
        lesson=lesson,
        quiz=quiz,
        results=results,
        score=score,
        completed=True,
        draft_answers={},
    )


@app.route("/progress")
@login_required
def progress():
    user = get_current_user()
    user_grade = user["grade"] if user and user["grade"] in GRADES else GRADES[0]
    selected_grade = request.args.get("grade", user_grade)
    if selected_grade not in GRADES:
        selected_grade = user_grade
    show_all_grades = request.args.get("view", "grade") == "all"
    progress_data = get_progress(user["id"])

    grouped_entries = {grade: [] for grade in GRADES}
    for lesson in LESSONS:
        lesson_grade = lesson.get("grade") or selected_grade
        if lesson_grade not in grouped_entries:
            grouped_entries[lesson_grade] = []
        if not show_all_grades and lesson.get("grade") and lesson_grade != selected_grade:
            continue

        status = progress_data.get(lesson["id"], {})
        grouped_entries[lesson_grade].append(
            {
                "title": lesson["title"],
                "subject": lesson["subject"],
                "completed": bool(status.get("completed", 0)),
                "score": status.get("score", None),
                "attempts": status.get("attempts", 0),
                "last_updated": status.get("last_updated", "Never"),
            }
        )

    visible_grades = list(GRADES) if show_all_grades else [selected_grade]
    grade_groups = []
    entries = []
    for grade in visible_grades:
        grade_entries = grouped_entries.get(grade, [])
        subject_map = {}
        for entry in grade_entries:
            subject = entry.get("subject", "General") or "General"
            subject_map.setdefault(subject, []).append(entry)

        subject_groups = []
        for subject_name in sorted(subject_map.keys()):
            subject_entries = subject_map[subject_name]
            subject_groups.append(
                {
                    "subject": subject_name,
                    "entries": subject_entries,
                    "completed_lessons": sum(1 for entry in subject_entries if entry["completed"]),
                    "average_score": round(sum(entry["score"] for entry in subject_entries if entry["score"] is not None) / max(1, sum(1 for entry in subject_entries if entry["score"] is not None))) if any(entry["score"] is not None for entry in subject_entries) else 0,
                }
            )
        grade_groups.append(
            {
                "grade": grade,
                "entries": grade_entries,
                "subjects": subject_groups,
                "completed_lessons": sum(1 for entry in grade_entries if entry["completed"]),
                "average_score": round(sum(entry["score"] for entry in grade_entries if entry["score"] is not None) / max(1, sum(1 for entry in grade_entries if entry["score"] is not None))) if any(entry["score"] is not None for entry in grade_entries) else 0,
            }
        )
        entries.extend(grade_entries)

    completed_scores = [entry["score"] for entry in entries if entry["score"] is not None]
    overall = {
        "completed_lessons": sum(1 for entry in entries if entry["completed"]),
        "total_lessons": len(entries),
        "average_score": round(sum(completed_scores) / max(1, len(completed_scores))) if completed_scores else 0,
    }
    return render_template(
        "progress.html",
        overall=overall,
        grade_groups=grade_groups,
        selected_grade=selected_grade,
        grade_options=GRADES,
        show_all_grades=show_all_grades,
    )


@app.route("/progress/export")
@login_required
def export_progress():
    user = get_current_user()
    user_grade = user["grade"] if user and user["grade"] in GRADES else GRADES[0]
    selected_grade = request.args.get("grade", user_grade)
    if selected_grade not in GRADES:
        selected_grade = user_grade
    show_all_grades = request.args.get("view", "grade") == "all"

    progress_data = get_progress(user["id"])
    rows = []
    for lesson in LESSONS:
        lesson_grade = lesson.get("grade") or selected_grade
        if not show_all_grades and lesson.get("grade") and lesson_grade != selected_grade:
            continue
        status = progress_data.get(lesson["id"], {})
        rows.append(
            {
                "grade": lesson_grade,
                "subject": lesson.get("subject", "General"),
                "lesson": lesson.get("title", "Untitled lesson"),
                "status": "Completed" if status.get("completed") else "Pending",
                "score": status.get("score", ""),
                "attempts": status.get("attempts", 0),
                "updated": status.get("last_updated", "Never"),
            }
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Grade", "Subject", "Lesson", "Status", "Score", "Attempts", "Updated"])
    for row in rows:
        writer.writerow([
            row["grade"],
            row["subject"],
            row["lesson"],
            row["status"],
            row["score"],
            row["attempts"],
            row["updated"],
        ])

    filename = "progress_all_grades.csv" if show_all_grades else f"progress_{selected_grade.lower().replace(' ', '_')}.csv"
    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@app.route("/ai", methods=["GET", "POST"])
@login_required
def ai_tutor():
    answer = None
    enhanced_status = None
    question_text = ""
    user = get_current_user()
    chat_history = normalize_history(session.get("ai_chat_history", []))
    online = is_online()
    enhanced = False
    enhanced_requested = False
    if request.method == "POST":
        if request.form.get("reset_chat"):
            chat_history = []
            session["ai_chat_history"] = chat_history
            return redirect(url_for("ai_tutor"))

        question_text = request.form.get("question", "").strip()
        enhanced_requested = request.form.get("enhanced", "false").lower() == "true"
        enhanced = enhanced_requested and OPENAI_AVAILABLE and online
        if question_text:
            if enhanced:
                messages = build_openai_enhanced_messages(question_text, history=chat_history)
                openai_answer, openai_error = call_openai(messages, model=OPENAI_ENHANCED_MODEL)
                if openai_answer:
                    answer = openai_answer
                    enhanced_status = f"Enhanced AI is active: OpenAI model '{OPENAI_ENHANCED_MODEL}' handled this response."
                else:
                    answer = get_ai_response(question_text, history=chat_history, enhanced=False, user=user)
                    enhanced_status = "Enhanced AI request failed, so fallback tutor was used."
                    if openai_error:
                        enhanced_status += f" ({openai_error})"
            else:
                answer = get_ai_response(question_text, history=chat_history, enhanced=False, user=user)
            if enhanced_requested and not enhanced:
                answer = (
                    "Enhanced AI is unavailable right now (internet or OpenAI key missing). "
                    "I answered using the local tutor instead.\n\n"
                ) + answer
                enhanced_status = "Enhanced AI is unavailable in current runtime (missing key, package, or internet)."
            if not answer or not answer.strip():
                answer = "I couldn't generate a reliable response this time. Please try again with a bit more detail."
            chat_history.append({"role": "user", "content": question_text})
            chat_history.append({"role": "assistant", "content": answer})
            session["ai_chat_history"] = normalize_history(chat_history)
            # Keep the input box empty after sending a message.
            question_text = ""
        else:
            answer = "Type your question or tell me what you'd like to discuss."
    return render_template(
        "ai_tutor.html",
        answer=answer,
        question_text=question_text,
        chat_history=chat_history,
        ai_enabled=OLLAMA_AVAILABLE or OPENAI_AVAILABLE,
        openai_available=OPENAI_AVAILABLE,
        offline_available=OLLAMA_AVAILABLE,
        online=online,
        enhanced=enhanced,
        enhanced_status=enhanced_status,
        openai_enhanced_model=OPENAI_ENHANCED_MODEL,
    )


@app.route("/offline")
def offline_page():
    return render_template("offline.html")


@app.route("/download/<lesson_id>")
@login_required
def download_lesson_pdf(lesson_id):
    lesson = LESSON_INDEX.get(lesson_id)
    if lesson is None:
        return redirect(url_for("index"))
    ensure_pdf_dir()
    create_pdf_for_lesson(lesson)
    return send_from_directory(PDF_DIR, f"{lesson_id}.pdf", as_attachment=True)


@app.route("/api/lessons")
def api_lessons():
    return jsonify(LESSONS)


@app.route("/api/quiz/<lesson_id>")
def api_quiz(lesson_id):
    quiz = QUIZ_INDEX.get(lesson_id)
    return jsonify(quiz if quiz else {})


if __name__ == "__main__":
    app.run(debug=True)
