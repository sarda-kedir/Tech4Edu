# Tech4Edu

Tech4Edu is an offline-first educational platform designed to empower students in underrepresented regions with structured STEM learning, guided practice, downloadable study materials, and AI-supported tutoring. It includes lesson viewing, quizzes, grade-based lesson access, progress tracking, PDF lesson downloads, and an AI tutor, all designed to keep working even when internet access is unreliable.

## Features

- Offline lesson viewer with modular content
- Downloadable lesson PDFs for offline reading
- Interactive quizzes with immediate feedback
- Local progress tracking via SQLite
- Student accounts with login, grade-based lesson access, and personalized progress data
- Simple AI tutor for lesson questions and study guidance
- Progressive Web App support for offline access and installability


## Setup

1. Create a Python environment:
   ```bash
   python -m venv venv
   .\venv\Scripts\activate
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the application:
   ```bash
   python app.py
   ```
4. Open your browser at `http://127.0.0.1:5000`

## Enhanced Online AI (OpenAI GPT)

The AI tutor can use OpenAI GPT when internet is available.

1. Create a `.env` file in the project root (or copy `.env.example`):

```bash
copy .env.example .env
```

2. Edit `.env` and set your real API key:

```env
OPENAI_API_KEY=your_real_openai_key
OPENAI_MODEL=gpt-4o-mini
```

3. Restart the app after updating environment values.

4. In the AI Tutor page, click **Use enhanced AI (requires internet)**.

If OpenAI is not configured, the app automatically falls back to local/offline tutor behavior.

## Optional Local AI Model

For stronger offline AI tutoring, install and configure Ollama locally so the app can use a local model without internet.

1. Install Ollama and a stronger compatible model, for example `qwen2.5:3b-instruct`.
2. Make sure the `ollama` Python package is installed:

```bash
pip install ollama
```

3. Optionally set the offline model name before running the app:

```bash
set OFFLINE_MODEL=qwen2.5:3b-instruct
```

4. Optional advanced controls:

```bash
set OFFLINE_MODEL_PINNED=1
set OFFLINE_MODEL_AUTO_PULL=1
set OLLAMA_AUTO_START=1
```

- `OFFLINE_MODEL_PINNED=1` forces the exact model in `OFFLINE_MODEL` first.
- `OFFLINE_MODEL_PINNED=0` lets the app auto-prefer stronger installed models.
- `OFFLINE_MODEL_AUTO_PULL=1` lets the app try pulling the configured model if none are installed.
- `OLLAMA_AUTO_START=1` lets the app try starting the local Ollama service automatically.

The app prefers local Ollama AI first, prioritizes stronger installed models by default, and falls back to internal responses if local AI is unavailable.

## Project Structure

- `app.py` — Flask application and backend logic
- `content/lessons.json` — lesson content data
- `content/quizzes.json` — offline quiz questions
- `templates/` — HTML templates for the web UI
- `static/` — CSS and PWA assets
- `static/pdfs/` — generated lesson PDFs

## Notes

- PDF resources are generated automatically for each lesson.
- The service worker caches key pages and static assets for offline use.
