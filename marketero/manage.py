"""Utilitário de linha de comando.

  python manage.py criar-usuario <usuario>     cria usuário (ou troca a senha) e mostra a senha gerada
"""
import secrets
import sys

from sqlalchemy import select

from app.db import db_session, init_db
from app.models import User
from app.security import hash_password


def criar_usuario(username: str) -> None:
    init_db()
    password = secrets.token_urlsafe(12)
    with db_session() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(username=username, password_hash="")
            db.add(user)
        user.password_hash = hash_password(password)
        db.commit()
    print(f"Usuário: {username}\nSenha: {password}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "criar-usuario":
        criar_usuario(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
