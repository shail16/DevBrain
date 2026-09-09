"""
DevBrain — authentication, users, sessions and the document registry.

Everything security-sensitive lives here so there is exactly one place to audit.

Three tables are created automatically on first start:

    devbrain_users      one row per account (password stored ONLY as a hash)
    devbrain_sessions   one row per active login, so a session can be revoked
    devbrain_documents  who uploaded which file, when, and what it is for

Why server-side sessions instead of stateless JWTs
--------------------------------------------------
The requirement is that deleting a user immediately stops them logging in *and*
using the app. A JWT stays valid until it expires, so a deleted user would keep
working for the remainder of the token's life unless you build a revocation list
anyway. A session row is checked (and JOINed against the user's `is_active`
flag) on every request, so revocation is instant and there is no second
mechanism to keep in sync.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import text

from config import Settings
from db import get_engine

# Cost factor for bcrypt. 12 is the common production default: slow enough that
# brute-forcing a stolen hash is impractical, fast enough (~250 ms) that logging
# in feels instant.
BCRYPT_ROUNDS = 12

SESSION_TTL_HOURS = 12
SESSION_COOKIE = "devbrain_session"

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 200          # bcrypt truncates past 72 BYTES; see hash_password
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 40


class AuthError(Exception):
    """Raised for any authentication or authorisation failure."""


@dataclass(frozen=True)
class User:
    id: int
    username: str
    full_name: str
    email: str
    is_admin: bool
    is_active: bool

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "full_name": self.full_name,
            "email": self.email,
            "is_admin": self.is_admin,
            "is_active": self.is_active,
        }


# --------------------------------------------------------------------------- #
#  Schema
# --------------------------------------------------------------------------- #

_SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS devbrain_users (
        id            SERIAL PRIMARY KEY,
        username      VARCHAR(40)  NOT NULL UNIQUE,
        full_name     VARCHAR(120) NOT NULL DEFAULT '',
        email         VARCHAR(200) NOT NULL DEFAULT '',
        password_hash VARCHAR(120) NOT NULL,
        is_admin      BOOLEAN      NOT NULL DEFAULT FALSE,
        is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        last_login_at TIMESTAMPTZ
    )
    """,
    # Usernames are compared case-insensitively so "Admin" and "admin" cannot
    # both exist and be confused for one another.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS devbrain_users_username_lower
        ON devbrain_users (lower(username))
    """,
    """
    CREATE TABLE IF NOT EXISTS devbrain_sessions (
        token_hash VARCHAR(64) PRIMARY KEY,
        user_id    INTEGER     NOT NULL REFERENCES devbrain_users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS devbrain_sessions_user
        ON devbrain_sessions (user_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS devbrain_documents (
        id             SERIAL PRIMARY KEY,
        source         VARCHAR(300) NOT NULL UNIQUE,
        file_type      VARCHAR(20)  NOT NULL DEFAULT '',
        purpose        TEXT         NOT NULL DEFAULT '',
        size_bytes     BIGINT       NOT NULL DEFAULT 0,
        chunks         INTEGER      NOT NULL DEFAULT 0,
        uploaded_by    VARCHAR(40)  NOT NULL DEFAULT 'system',
        uploaded_by_id INTEGER      REFERENCES devbrain_users(id) ON DELETE SET NULL,
        uploaded_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
]


def init_schema(settings: Settings) -> None:
    """Create the auth/registry tables. Safe to call on every start-up."""
    with get_engine(settings).begin() as connection:
        for statement in _SCHEMA_SQL:
            connection.execute(text(statement))


# --------------------------------------------------------------------------- #
#  Passwords
# --------------------------------------------------------------------------- #


def hash_password(password: str) -> str:
    """
    Hash a password with bcrypt.

    bcrypt silently ignores everything past 72 *bytes*, which would make two
    different long passwords interchangeable. Pre-hashing with SHA-256 first
    collapses any length into a fixed 64-character digest, so the full password
    always contributes to the result.
    """
    prepared = hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")
    return bcrypt.hashpw(prepared, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    prepared = hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")
    try:
        return bcrypt.checkpw(prepared, password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # Malformed hash in the database — treat as a failed login, never a crash.
        return False


def validate_credentials(username: str, password: str) -> tuple[str, str]:
    """Normalise and sanity-check a new account's credentials."""
    username = (username or "").strip()
    password = password or ""

    if not (MIN_USERNAME_LENGTH <= len(username) <= MAX_USERNAME_LENGTH):
        raise AuthError(
            f"Username must be {MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} characters."
        )
    if not all(c.isalnum() or c in "._-" for c in username):
        raise AuthError("Username may only contain letters, digits, dot, underscore and hyphen.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    return username, password


# --------------------------------------------------------------------------- #
#  Users
# --------------------------------------------------------------------------- #

_USER_COLUMNS = "id, username, full_name, email, is_admin, is_active"


def _row_to_user(row) -> User:
    return User(
        id=int(row[0]),
        username=str(row[1]),
        full_name=str(row[2] or ""),
        email=str(row[3] or ""),
        is_admin=bool(row[4]),
        is_active=bool(row[5]),
    )


def create_user(
    settings: Settings,
    username: str,
    password: str,
    full_name: str = "",
    email: str = "",
    is_admin: bool = False,
) -> User:
    username, password = validate_credentials(username, password)

    with get_engine(settings).begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM devbrain_users WHERE lower(username) = lower(:u)"),
            {"u": username},
        ).scalar()
        if exists:
            raise AuthError(f"The username '{username}' is already taken.")

        row = connection.execute(
            text(
                f"""
                INSERT INTO devbrain_users (username, full_name, email, password_hash, is_admin)
                VALUES (:u, :f, :e, :p, :a)
                RETURNING {_USER_COLUMNS}
                """
            ),
            {
                "u": username,
                "f": (full_name or "").strip()[:120],
                "e": (email or "").strip()[:200],
                "p": hash_password(password),
                "a": is_admin,
            },
        ).one()
    return _row_to_user(row)


def get_user_by_username(settings: Settings, username: str) -> User | None:
    with get_engine(settings).connect() as connection:
        row = connection.execute(
            text(f"SELECT {_USER_COLUMNS} FROM devbrain_users WHERE lower(username) = lower(:u)"),
            {"u": (username or "").strip()},
        ).first()
    return _row_to_user(row) if row else None


def list_users(settings: Settings) -> list[dict]:
    with get_engine(settings).connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT u.id, u.username, u.full_name, u.email, u.is_admin, u.is_active,
                       u.created_at, u.last_login_at,
                       (SELECT count(*) FROM devbrain_documents d WHERE d.uploaded_by_id = u.id)
                FROM devbrain_users u
                ORDER BY u.is_admin DESC, lower(u.username)
                """
            )
        ).all()

    return [
        {
            "id": int(r[0]),
            "username": str(r[1]),
            "full_name": str(r[2] or ""),
            "email": str(r[3] or ""),
            "is_admin": bool(r[4]),
            "is_active": bool(r[5]),
            "created_at": r[6].isoformat() if r[6] else None,
            "last_login_at": r[7].isoformat() if r[7] else None,
            "documents": int(r[8] or 0),
        }
        for r in rows
    ]


def delete_user(settings: Settings, user_id: int) -> str:
    """
    Delete a user and every session they hold.

    The ON DELETE CASCADE on devbrain_sessions means their active logins die
    with the row, so a deleted user is locked out immediately rather than at the
    end of their session. Their uploaded documents are deliberately kept — the
    knowledge base should not lose content because someone left — but
    `uploaded_by_id` becomes NULL while the `uploaded_by` name is retained for
    the audit trail.

    The last remaining admin cannot be deleted; that would leave the system with
    no way to manage users at all.
    """
    with get_engine(settings).begin() as connection:
        row = connection.execute(
            text("SELECT username, is_admin FROM devbrain_users WHERE id = :i"),
            {"i": user_id},
        ).first()
        if row is None:
            raise AuthError("That user no longer exists.")

        username, is_admin = str(row[0]), bool(row[1])

        if is_admin:
            remaining = connection.execute(
                text("SELECT count(*) FROM devbrain_users WHERE is_admin AND id <> :i"),
                {"i": user_id},
            ).scalar_one()
            if int(remaining) == 0:
                raise AuthError(
                    "Cannot delete the only administrator — you would lock everyone "
                    "out of user management."
                )

        connection.execute(text("DELETE FROM devbrain_users WHERE id = :i"), {"i": user_id})
    return username


def ensure_admin(settings: Settings) -> str | None:
    """
    Create the administrator account from ADMIN_USERNAME / ADMIN_PASSWORD.

    Runs on every start-up but only ever *creates* — an existing admin is left
    alone, so changing the password in the app is not silently reverted on the
    next restart. Returns the username if one was created, else None.
    """
    username = (os.getenv("ADMIN_USERNAME") or "").strip()
    password = os.getenv("ADMIN_PASSWORD") or ""
    if not username or not password:
        return None

    with get_engine(settings).connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM devbrain_users WHERE lower(username) = lower(:u)"),
            {"u": username},
        ).scalar()
    if exists:
        return None

    create_user(
        settings,
        username=username,
        password=password,
        full_name=(os.getenv("ADMIN_FULL_NAME") or "Administrator").strip(),
        email=(os.getenv("ADMIN_EMAIL") or "").strip(),
        is_admin=True,
    )
    return username


# --------------------------------------------------------------------------- #
#  Sessions
# --------------------------------------------------------------------------- #


def _hash_token(token: str) -> str:
    """
    Store only a hash of the session token.

    If the database is ever dumped or leaked, the raw tokens are not in it, so
    nobody can resume a live session from a backup. This is the same reasoning
    that applies to passwords.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def login(settings: Settings, username: str, password: str) -> tuple[str, User]:
    """
    Verify credentials and open a session. Returns (raw_token, user).

    Every failure path raises the same message on purpose: telling an attacker
    whether the username exists ("no such user" vs "wrong password") hands them
    a way to enumerate valid accounts.
    """
    generic = AuthError("Incorrect username or password.")
    username = (username or "").strip()

    with get_engine(settings).connect() as connection:
        row = connection.execute(
            text(
                f"SELECT {_USER_COLUMNS}, password_hash "
                "FROM devbrain_users WHERE lower(username) = lower(:u)"
            ),
            {"u": username},
        ).first()

    if row is None:
        # Spend roughly the same time as a real verification so the response
        # time cannot be used to tell existing usernames from missing ones.
        verify_password(password, "$2b$12$" + "." * 53)
        raise generic

    user = _row_to_user(row)
    if not verify_password(password, str(row[6])):
        raise generic
    if not user.is_active:
        raise AuthError("This account has been disabled. Contact the administrator.")

    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)

    with get_engine(settings).begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devbrain_sessions (token_hash, user_id, expires_at) "
                "VALUES (:t, :u, :e)"
            ),
            {"t": _hash_token(token), "u": user.id, "e": expires},
        )
        connection.execute(
            text("UPDATE devbrain_users SET last_login_at = now() WHERE id = :i"),
            {"i": user.id},
        )
        # Opportunistic cleanup of expired rows.
        connection.execute(text("DELETE FROM devbrain_sessions WHERE expires_at < now()"))

    return token, user


def user_for_token(settings: Settings, token: str | None) -> User | None:
    """
    Resolve a session token to a live user, or None.

    The JOIN plus `is_active` check is what makes deletion and disabling take
    effect instantly: the row simply stops resolving on the very next request.
    """
    if not token:
        return None
    with get_engine(settings).connect() as connection:
        row = connection.execute(
            text(
                f"""
                SELECT {', '.join('u.' + c for c in _USER_COLUMNS.split(', '))}
                FROM devbrain_sessions s
                JOIN devbrain_users u ON u.id = s.user_id
                WHERE s.token_hash = :t
                  AND s.expires_at > now()
                  AND u.is_active
                """
            ),
            {"t": _hash_token(token)},
        ).first()
    return _row_to_user(row) if row else None


def logout(settings: Settings, token: str | None) -> None:
    if not token:
        return
    with get_engine(settings).begin() as connection:
        connection.execute(
            text("DELETE FROM devbrain_sessions WHERE token_hash = :t"),
            {"t": _hash_token(token)},
        )


# --------------------------------------------------------------------------- #
#  Document registry
# --------------------------------------------------------------------------- #


def record_document(
    settings: Settings,
    source: str,
    file_type: str,
    purpose: str,
    size_bytes: int,
    chunks: int,
    user: User | None,
) -> None:
    """
    Record (or refresh) who owns a document.

    Keyed on `source`, which is the same value stored in every chunk's metadata,
    so the registry and the vector store always agree about what a document is
    called. Re-uploading updates ownership to whoever uploaded it last.
    """
    with get_engine(settings).begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO devbrain_documents
                    (source, file_type, purpose, size_bytes, chunks,
                     uploaded_by, uploaded_by_id, uploaded_at)
                VALUES (:s, :t, :p, :b, :c, :un, :ui, now())
                ON CONFLICT (source) DO UPDATE SET
                    file_type      = EXCLUDED.file_type,
                    purpose        = CASE WHEN EXCLUDED.purpose <> ''
                                          THEN EXCLUDED.purpose
                                          ELSE devbrain_documents.purpose END,
                    size_bytes     = EXCLUDED.size_bytes,
                    chunks         = EXCLUDED.chunks,
                    uploaded_by    = EXCLUDED.uploaded_by,
                    uploaded_by_id = EXCLUDED.uploaded_by_id,
                    uploaded_at    = now()
                """
            ),
            {
                "s": source,
                "t": file_type,
                "p": (purpose or "").strip()[:2000],
                "b": size_bytes,
                "c": chunks,
                "un": user.username if user else "system",
                "ui": user.id if user else None,
            },
        )


def get_document_owner(settings: Settings, source: str) -> tuple[str, str | None]:
    """Return (uploaded_by, uploaded_at_iso) for a source, defaulting to system."""
    with get_engine(settings).connect() as connection:
        row = connection.execute(
            text("SELECT uploaded_by, uploaded_at FROM devbrain_documents WHERE source = :s"),
            {"s": source},
        ).first()
    if row is None:
        return "system", None
    return str(row[0]), row[1].isoformat() if row[1] else None


def list_documents(settings: Settings) -> list[dict]:
    with get_engine(settings).connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT source, file_type, purpose, size_bytes, chunks,
                       uploaded_by, uploaded_at
                FROM devbrain_documents
                ORDER BY uploaded_at DESC
                """
            )
        ).all()
    return [
        {
            "source": str(r[0]),
            "file_type": str(r[1] or ""),
            "purpose": str(r[2] or ""),
            "size_bytes": int(r[3] or 0),
            "chunks": int(r[4] or 0),
            "uploaded_by": str(r[5] or "system"),
            "uploaded_at": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]


def forget_document(settings: Settings, source: str) -> None:
    """Remove a document's registry row (called when the file is deleted)."""
    with get_engine(settings).begin() as connection:
        connection.execute(
            text("DELETE FROM devbrain_documents WHERE source = :s"), {"s": source}
        )


def list_documents_for_user(settings: Settings, user_id: int) -> list[dict]:
    """
    Documents uploaded by one specific person.

    Matched on `uploaded_by_id` rather than the username string so a renamed
    account keeps its history. Rows whose owner was deleted have a NULL id and
    correctly stop appearing for everyone.
    """
    with get_engine(settings).connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT source, file_type, purpose, size_bytes, chunks, uploaded_at
                FROM devbrain_documents
                WHERE uploaded_by_id = :i
                ORDER BY uploaded_at DESC
                """
            ),
            {"i": user_id},
        ).all()
    return [
        {
            "source": str(r[0]),
            "file_type": str(r[1] or ""),
            "purpose": str(r[2] or ""),
            "size_bytes": int(r[3] or 0),
            "chunks": int(r[4] or 0),
            "uploaded_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]
