"""Teacher routes for managing groups and their students."""

from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from .extensions import db
from .models import Group, User, group_students

groups_bp = Blueprint("groups", __name__)


def teacher_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "teacher":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def owned_group(group_id):
    group = db.session.get(Group, group_id)
    if group is None:
        abort(404)
    if group.teacher_id != current_user.id:
        abort(403)
    return group


@groups_bp.route("/teacher/groups")
@teacher_required
def teacher_groups():
    groups = (
        db.session.query(Group, func.count(group_students.c.student_id).label("student_count"))
        .outerjoin(group_students, Group.id == group_students.c.group_id)
        .filter(Group.teacher_id == current_user.id)
        .group_by(Group.id)
        .order_by(Group.id)
        .all()
    )
    rows = [{"id": group.id, "title": group.title, "student_count": count} for group, count in groups]
    return render_template("teacher_groups.html", groups=rows)


@groups_bp.route("/teacher/groups/new", methods=["GET", "POST"])
@teacher_required
def teacher_group_new():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("Введите название группы", "error")
            return render_template("teacher_group_form.html"), 400
        db.session.add(Group(title=title, teacher_id=current_user.id))
        db.session.commit()
        flash("Группа создана", "success")
        return redirect(url_for("groups.teacher_groups"))
    return render_template("teacher_group_form.html")


@groups_bp.route("/teacher/groups/<int:group_id>")
@teacher_required
def teacher_group_detail(group_id):
    group = owned_group(group_id)
    available_students = (
        User.query.filter_by(role="student")
        .filter(~User.student_groups.any(Group.id == group.id))
        .order_by(User.full_name)
        .all()
    )
    members = sorted(group.students, key=lambda student: (student.full_name.casefold(), student.id))
    return render_template(
        "teacher_group_detail.html", group=group, members=members, available_students=available_students
    )


@groups_bp.route("/teacher/groups/<int:group_id>/members", methods=["POST"])
@teacher_required
def teacher_group_add_member(group_id):
    group = owned_group(group_id)
    student_id = request.form.get("student_id", type=int)
    student = db.session.get(User, student_id) if student_id else None
    if student is None or student.role != "student":
        flash("Выберите существующего студента", "error")
        return redirect(url_for("groups.teacher_group_detail", group_id=group.id))
    if student not in group.students:
        group.students.append(student)
        db.session.commit()
    flash("Студент добавлен в группу", "success")
    return redirect(url_for("groups.teacher_group_detail", group_id=group.id))
