# ChatDB SelfService MVP (Part 1)

This repository contains the first MVP slice:
- Telegram chatbot skeleton.
- Database initialization (SQLite by default).

## 1) Setup

1. Create and activate virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Fill in `config.yaml` (copy from `config.example.yaml`).

## 2) Initialize database

```bash
python -m app.init_db
```

This command:
- creates tables `roles`, `users`, and `visibility_groups`;
- seeds roles `Admin` and `DefaultUser`;
- seeds visibility group `Administrators`;
- creates initial superuser from `initial_superuser_name`.

## 3) Run bot

```bash
python main.py
```

## Available commands

- `/start` - binds Telegram account to existing DB user by full name (first launch), then greets.
- `/ping` - health check.
- `/whoami` - returns current user from DB.
