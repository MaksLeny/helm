"""
Helm — генератор хеша пароля.

Запусти этот скрипт, введи желаемый пароль, и он напечатает строку хеша.
Вставь её в core/config.py в значение _DEFAULT_PASSWORD_HASH, либо задай
переменной окружения HELM_PASSWORD_HASH.

    python set_password.py
"""
import getpass
import sys

# Импортируем функцию хеширования из ядра
sys.path.insert(0, ".")
from core.auth import hash_password  # noqa: E402


def main() -> None:
    print("Helm — установка пароля")
    print("-" * 40)
    pw1 = getpass.getpass("Новый пароль: ")
    if not pw1:
        print("Пустой пароль — отмена.")
        return
    pw2 = getpass.getpass("Повтори пароль: ")
    if pw1 != pw2:
        print("Пароли не совпадают — отмена.")
        return

    h = hash_password(pw1)
    print()
    print("Готово. Вставь эту строку в core/config.py:")
    print()
    print(f'_DEFAULT_PASSWORD_HASH: str = "{h}"')
    print()
    print("Либо задай переменную окружения перед запуском:")
    print(f'    set HELM_PASSWORD_HASH={h}')


if __name__ == "__main__":
    main()
