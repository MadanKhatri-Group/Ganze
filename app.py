from __future__ import annotations

import json
import os
import secrets
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash


DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "store.json"
_store_lock = Lock()

_geo_cache = {}
_geo_ttl_seconds = 10 * 60


login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message_category = "info"


class User(UserMixin):
    def __init__(self, user_id: int, name: str, email: str, password_hash: str, created_at: datetime):
        self.id = user_id
        self.name = name
        self.email = email
        self.password_hash = password_hash
        self.created_at = created_at

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value):
    try:
        if value and isinstance(value, str) and value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return datetime.now(timezone.utc)


def load_store():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not DATA_FILE.exists():
        save_store({"users": [], "tasks": {}})

    with _store_lock:
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"users": [], "tasks": {}}


def save_store(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = DATA_FILE.with_suffix(".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    with _store_lock:
        temp_file.write_text(payload, encoding="utf-8")
        os.replace(temp_file, DATA_FILE)


def get_user_by_id(user_id):
    data = load_store()
    for raw in data.get("users", []):
        if raw.get("id") == user_id:
            return User(
                user_id=raw["id"],
                name=raw.get("name", ""),
                email=raw.get("email", ""),
                password_hash=raw.get("password_hash", ""),
                created_at=_parse_iso(raw.get("created_at", "")),
            )
    return None


def get_user_raw_by_email(email):
    data = load_store()
    for raw in data.get("users", []):
        if str(raw.get("email", "")).lower() == email.lower():
            return raw
    return None


def add_user(name: str, email: str, password: str) -> User:
    data = load_store()
    users: list[dict[str, Any]] = list(data.get("users", []))
    next_id = (max([int(u.get("id", 0)) for u in users], default=0) + 1) if users else 1

    user = User(user_id=next_id, name=name, email=email, password_hash="", created_at=datetime.now(timezone.utc))
    user.set_password(password)

    users.append(
        {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "password_hash": user.password_hash,
            "created_at": _now_iso(),
        }
    )
    data["users"] = users
    data.setdefault("tasks", {})
    data["tasks"].setdefault(str(user.id), [])
    save_store(data)
    return user


def list_tasks(user_id):
    data = load_store()
    tasks = data.get("tasks", {}).get(str(user_id), [])
    return list(tasks) if isinstance(tasks, list) else []


def add_task(user_id: int, text: str) -> None:
    data = load_store()

    tasks = data.get("tasks", {}).get(str(user_id), [])
    if not isinstance(tasks, list):
        tasks = []

    new_task = {
        "id": secrets.token_urlsafe(8),
        "text": text,
        "done": False,
        "created_at": _now_iso(),
    }

    tasks.insert(0, new_task)

    if "tasks" not in data:
        data["tasks"] = {}
    data["tasks"][str(user_id)] = tasks

    save_store(data)


def toggle_task(user_id: int, task_id: str) -> None:
    data = load_store()
    tasks = list_tasks(user_id)
    for task in tasks:
        if str(task.get("id")) == task_id:
            task["done"] = not bool(task.get("done"))
            break
    data.setdefault("tasks", {})
    data["tasks"][str(user_id)] = tasks
    save_store(data)


def is_safe_next_url(next_url: str | None) -> bool:
    if not next_url:
        return False
    parsed = urlparse(next_url)
    return parsed.scheme == "" and parsed.netloc == ""


def get_client_ip() -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or ""


def is_private_ip(ip: str) -> bool:
    if not ip:
        return True

    if ip == "127.0.0.1" or ip == "::1":
        return True

    if ip.startswith("127."):
        return True

    if ip.startswith("192.168.") or ip.startswith("10."):
        return True

    if ip.startswith("172."):
        parts = ip.split(".")
        if len(parts) > 1:
            try:
                second = int(parts[1])
                if 16 <= second <= 31:
                    return True
            except Exception:
                pass

    return False


def lookup_ip_location(ip: str) -> dict[str, Any] | None:
    if not ip or is_private_ip(ip):
        return None

    current_time = time.time()

    if ip in _geo_cache:
        cached_time, cached_data = _geo_cache[ip]
        if current_time - cached_time < _geo_ttl_seconds:
            return cached_data

    try:
        response = requests.get(f"http://ip-api.com/json/{ip}", timeout=2)
        data = response.json()
        if data.get("status") != "success":
            return None

        result = {
            "country": data.get("country", ""),
            "region": data.get("regionName", ""),
            "city": data.get("city", ""),
            "isp": data.get("isp", ""),
        }

        _geo_cache[ip] = (current_time, result)
        return result

    except Exception:
        return None


def generate_password(length, use_numbers=True, use_symbols=True):
    try:
        length = int(length)
    except Exception:
        length = 12

    if length < 8:
        length = 8
    elif length > 64:
        length = 64

    chars = list(string.ascii_letters)
    if use_numbers:
        chars.extend(string.digits)
    if use_symbols:
        chars.extend("!@#$%^&*_-+?")

    password = []
    for _ in range(length):
        password.append(secrets.choice(chars))

    return "".join(password)


@login_manager.user_loader
def load_user(user_id: str):
    if not user_id.isdigit():
        return None
    return get_user_by_id(int(user_id))


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    login_manager.init_app(app)

    @app.errorhandler(404)
    def not_found(_err):
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def server_error(_err):
        return render_template("500.html"), 500

    @app.get("/")
    def home():
        return render_template("home.html")

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""

            if not name or not email or not password:
                flash("Please fill in all fields.", "warning")
                return render_template("signup.html", name=name, email=email)

            if len(password) < 6:
                flash("Password should be at least 6 characters.", "warning")
                return render_template("signup.html", name=name, email=email)

            existing = get_user_raw_by_email(email)
            if existing:
                flash("That email is already registered. Try logging in.", "info")
                return redirect(url_for("login"))

            user = add_user(name=name, email=email, password=password)
            login_user(user)
            flash("Welcome! Your account is ready.", "success")
            return redirect(url_for("dashboard"))

        return render_template("signup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""

            raw = get_user_raw_by_email(email)
            if not raw:
                flash("Email or password is incorrect.", "danger")
                return render_template("login.html", email=email)

            user = User(
                user_id=int(raw["id"]),
                name=str(raw.get("name", "")),
                email=str(raw.get("email", "")),
                password_hash=str(raw.get("password_hash", "")),
                created_at=_parse_iso(str(raw.get("created_at", ""))),
            )

            if not user.check_password(password):
                flash("Email or password is incorrect.", "danger")
                return render_template("login.html", email=email)

            login_user(user)
            flash("You’re logged in.", "success")

            next_url = request.args.get("next")
            if is_safe_next_url(next_url):
                return redirect(next_url)
            return redirect(url_for("dashboard"))

        return render_template("login.html")

    @app.route("/dashboard", methods=["GET", "POST"])
    @login_required
    def dashboard():
        generated = None

        if request.method == "POST":
            action = request.form.get("action", "").strip()
            user_id = int(current_user.id)

            if action == "add_task":
                text = (request.form.get("task_text") or "").strip()
                if text:
                    add_task(user_id, text)
                    flash("Task added.", "success")
                else:
                    flash("Write something first.", "warning")
                return redirect(url_for("dashboard"))

            elif action == "toggle_task":
                task_id = request.form.get("task_id")
                if task_id:
                    toggle_task(user_id, task_id)
                return redirect(url_for("dashboard"))

            elif action == "gen_password":
                length = request.form.get("length", 14)
                numbers = request.form.get("numbers") == "on"
                symbols = request.form.get("symbols") == "on"
                generated = generate_password(length, numbers, symbols)

        ip = get_client_ip()
        location = lookup_ip_location(ip)
        tasks = list_tasks(int(current_user.id))

        return render_template(
            "dashboard.html",
            tasks=tasks,
            generated_password=generated,
            client_ip=ip,
            location=location,
        )

    @app.post("/logout")
    @login_required
    def logout():
        logout_user()
        flash("Logged out. See you soon!", "info")
        return redirect(url_for("home"))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
