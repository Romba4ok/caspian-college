"""Database guards shared by Alembic and isolated create_all fixtures."""


def install_guards(connection):
    if connection.dialect.name == "sqlite":
        statements = (
            """CREATE TRIGGER task_assignees_not_new_insert BEFORE INSERT ON task_assignees
               WHEN EXISTS (SELECT 1 FROM tasks WHERE id=NEW.task_id AND status='new')
               BEGIN SELECT RAISE(ABORT, 'new task cannot have assignees'); END""",
            """CREATE TRIGGER task_assignees_not_new_update BEFORE UPDATE ON task_assignees
               WHEN EXISTS (SELECT 1 FROM tasks WHERE id=NEW.task_id AND status='new')
               BEGIN SELECT RAISE(ABORT, 'new task cannot have assignees'); END""",
            """CREATE TRIGGER tasks_new_without_assignees BEFORE UPDATE OF status ON tasks
               WHEN NEW.status='new' AND EXISTS (SELECT 1 FROM task_assignees WHERE task_id=NEW.id)
               BEGIN SELECT RAISE(ABORT, 'new task cannot have assignees'); END""",
        )
    elif connection.dialect.name == "postgresql":
        statements = (
            """CREATE FUNCTION portal_guard_task_assignees() RETURNS trigger LANGUAGE plpgsql AS $$
               DECLARE current_status text;
               BEGIN
                 SELECT status INTO current_status FROM tasks WHERE id=NEW.task_id FOR UPDATE;
                 IF current_status='new' THEN RAISE EXCEPTION 'new task cannot have assignees' USING ERRCODE='23514'; END IF;
                 RETURN NEW;
               END $$""",
            """CREATE TRIGGER task_assignees_not_new BEFORE INSERT OR UPDATE ON task_assignees
               FOR EACH ROW EXECUTE FUNCTION portal_guard_task_assignees()""",
            """CREATE FUNCTION portal_guard_new_task() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN
                 IF NEW.status='new' AND EXISTS (SELECT 1 FROM task_assignees WHERE task_id=NEW.id)
                 THEN RAISE EXCEPTION 'new task cannot have assignees' USING ERRCODE='23514'; END IF;
                 RETURN NEW;
               END $$""",
            """CREATE TRIGGER tasks_new_without_assignees BEFORE UPDATE OF status ON tasks
               FOR EACH ROW EXECUTE FUNCTION portal_guard_new_task()""",
        )
    else:
        raise RuntimeError("Unsupported assignment database")
    for statement in statements:
        connection.exec_driver_sql(statement)
