"""Login and logout routes."""

from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user

from .models import User

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if (user and user.is_active and user.role in ("admin", "head", "employee")
                and (user.role == "admin" or user.department_id is not None)
                and user.check_password(password) and login_user(user)):
            next_url = request.args.get("next", "")
            parsed_next = urlparse(next_url)
            if (
                parsed_next.scheme == ""
                and parsed_next.netloc == ""
                and next_url.startswith("/")
                and not next_url.startswith("//")
                and "\\" not in next_url
            ):
                return redirect(next_url)
            return redirect(url_for("index"))
        flash("Неверный логин или пароль", "error")
    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"])
def logout():
    logout_user()
    flash("Вы вышли из системы", "success")
    return redirect(url_for("auth.login"))
