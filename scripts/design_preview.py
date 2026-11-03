"""Запуск тестовой копии сайта с демо-данными для просмотра дизайна.

Использует tests/support/navigation_smoke.py: временная база, ваши рабочие данные не затрагиваются.
Остановка: Ctrl+C или закрытие окна — временные файлы удаляются.
"""
import json
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "tests" / "support" / "navigation_smoke.py"


def main():
    process = subprocess.Popen([sys.executable, "-X", "utf8", str(SMOKE)], cwd=ROOT, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    first = process.stdout.readline()
    try:
        info = json.loads(first)
    except ValueError:
        print("Не удалось запустить тестовый сайт:", first or "нет ответа")
        process.kill()
        return 1
    stop = Path(info["stop_file"])
    print("Тестовый сайт запущен:", info["url"])
    print("Пароль для всех:", info["password"])
    print("Логины: smoke-head (начальник), smoke-admin (администратор), smoke-employee (сотрудник)")
    print("Остановка: Ctrl+C или закройте это окно.")
    webbrowser.open(info["url"] + "/login")
    try:
        while process.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            stop.touch()
            process.wait(timeout=10)
        except Exception:
            process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
