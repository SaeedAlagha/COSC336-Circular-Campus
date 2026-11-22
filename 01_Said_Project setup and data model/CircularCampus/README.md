# Circular Campus

Intelligent, AI-Powered Circular Campus Resource Exchange and Asset Life Cycle Management System
(COSC 336, Phase 6). The application is built step by step; see the commit history.

This first step contains the project settings and the complete data model from the Phase 4-5 ERD
(Figures 20-30) with its migrations.

    python -m venv .venv
    .venv\Scripts\activate          (macOS: source .venv/bin/activate)
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py check
