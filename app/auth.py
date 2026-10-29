"""Login and logout routes."""

from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for, session
from flask_login import current_user, login_user, logout_user

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from .extensions import db
from .models import User
from .permissions import is_platform_user
from .accounts import authentication_stamp

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            # Serialize a successful login with disable/restore and password reset.
            user = db.session.execute(select(User).where(User.username == username).with_for_update(key_share=True)
                .execution_options(populate_existing=True)).scalar_one_or_none()
            accepted = is_platform_user(user) and user.check_password(password) and login_user(user)
            if accepted:
                session["_auth_stamp"] = authentication_stamp(user)
                db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logout_user()
            session.pop("_auth_stamp",None)
            flash("Не удалось выполнить вход. Повторите попытку", "error")
            return render_template("login.html"),500
        if accepted:
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
    session.pop("_auth_stamp",None)
    flash("Вы вышли из системы", "success")
    return redirect(url_for("auth.login"))
