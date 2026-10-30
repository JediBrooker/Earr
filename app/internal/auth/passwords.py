"""Hashing and checking passwords.

Kept apart from authentication.py so the Audiobookshelf sign-in can use them
without the two importing each other.
"""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlmodel import Session

from app.internal.models import GroupEnum, User

ph = PasswordHasher()


def is_correct_password(user: User, password: str) -> bool:
    try:
        return ph.verify(user.password, password)
    except VerifyMismatchError:
        return False


def authenticate_user(session: Session, username: str, password: str) -> User | None:
    user = session.get(User, username)
    if not user:
        return None

    try:
        ph.verify(user.password, password)
    except VerifyMismatchError:
        return None

    if ph.check_needs_rehash(user.password):
        user.password = ph.hash(password)
        session.add(user)
        session.commit()

    return user


def create_user(
    username: str,
    password: str,
    group: GroupEnum = GroupEnum.untrusted,
    root: bool = False,
    extra_data: str | None = None,
) -> User:
    password_hash = ph.hash(password)
    return User(
        username=username,
        password=password_hash,
        group=group,
        root=root,
        extra_data=extra_data,
    )
