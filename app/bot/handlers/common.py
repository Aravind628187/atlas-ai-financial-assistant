from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User


def get_or_create_user(db: Session, telegram_id: int, username: str | None, first_name: str | None) -> tuple[User, bool]:
    user = db.execute(select(User).where(User.telegram_id == telegram_id)).scalar_one_or_none()
    if user:
        if username and user.username != username:
            user.username = username
        return user, False

    user = User(telegram_id=telegram_id, username=username, first_name=first_name)
    db.add(user)
    db.flush()
    return user, True


def chunk_for_telegram(text: str, limit: int = 3800) -> list[str]:
    """Telegram caps messages at 4096 chars; keep a safety margin and split on paragraph breaks."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 > limit:
            if current:
                chunks.append(current.strip())
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current.strip())
    return chunks
