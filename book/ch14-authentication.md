# Chapter 14 — Authentication and Sessions

---

## 14.1 The requirements

DevBrain now needs to be multi-user, with specific behaviour:

1. Nobody can search or upload while signed out
2. Anyone can create a standard account
3. Exactly **one** administrator, seeded from configuration
4. Administrators can delete users
5. **A deleted user must lose access immediately** — not when their token expires

Requirement 5 looks like a detail. It determines the entire architecture.

Everything security-sensitive lives in `auth.py` (577 lines) so there is exactly
one place to audit.

---

## 14.2 Sessions or JWTs — and why the answer is forced

This is the central design decision, and requirement 5 settles it.

### How a JWT works

A JSON Web Token is **self-contained**. It carries the user's identity plus a
signature. The server verifies the signature and trusts the contents — no database
lookup.

That is its great advantage: stateless, fast, horizontally scalable.

It is also the problem. **The server has no record of issued tokens, so it cannot
revoke one.** Delete a user and their token keeps working until it expires. With a
12-hour expiry, a deleted account retains full access for up to 12 hours.

The usual workaround is a revocation list — a database table of blacklisted tokens,
checked on every request. But at that point you have a database lookup per request
*and* a second mechanism to keep in sync. You have paid the cost of sessions while
keeping the complexity of JWTs.

### How server-side sessions work

Store a row per active login. Check it on every request:

```sql
SELECT u.* FROM devbrain_sessions s
JOIN devbrain_users u ON u.id = s.user_id
WHERE s.token_hash = :t
  AND s.expires_at > now()
  AND u.is_active
```

Delete the user and `ON DELETE CASCADE` takes their sessions with it. The row stops
resolving on the **very next request**. Revocation is instant, with one mechanism.

### The verdict

| | JWT | Session |
|---|---|---|
| Lookup per request | None | One indexed query |
| Instant revocation | **No** | **Yes** |
| Horizontal scaling | Trivial | Needs shared DB (we have one) |
| Mechanisms to maintain | 1 (or 2 with blacklist) | 1 |

**For DevBrain, sessions win outright.** We already have Postgres, the lookup is a
primary-key hit, and the requirement demands revocation.

> **The general lesson:** JWTs are excellent for short-lived, high-volume,
> genuinely stateless auth. The moment you need revocation, their main advantage
> evaporates. Choose based on your revocation requirement, not on fashion.

---

## 14.3 The schema

```sql
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
);

CREATE UNIQUE INDEX IF NOT EXISTS devbrain_users_username_lower
    ON devbrain_users (lower(username));

CREATE TABLE IF NOT EXISTS devbrain_sessions (
    token_hash VARCHAR(64) PRIMARY KEY,
    user_id    INTEGER     NOT NULL REFERENCES devbrain_users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
```

Four details that matter:

**`password_hash`, never `password`.** The column name is documentation.

**The lowercase unique index.** `VARCHAR UNIQUE` would allow both `Admin` and
`admin` — a genuine impersonation risk. Indexing `lower(username)` makes usernames
case-insensitively unique.

**`ON DELETE CASCADE` on `user_id`.** This is requirement 5, expressed in DDL.
Delete a user; their sessions vanish atomically.

**`token_hash` is the primary key**, not the token. See §14.5.

---

## 14.4 Password hashing

```python
BCRYPT_ROUNDS = 12

def hash_password(password: str) -> str:
    """
    bcrypt silently ignores everything past 72 *bytes*, which would make two
    different long passwords interchangeable. Pre-hashing with SHA-256 first
    collapses any length into a fixed 64-character digest.
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
```

### Why bcrypt and not SHA-256 alone

A general-purpose hash like SHA-256 is designed to be **fast**. That is exactly
wrong for passwords: an attacker with a stolen database can try billions of
candidates per second on a GPU.

bcrypt is deliberately **slow** and the cost is tunable. `rounds=12` means 2¹²
iterations — about 250ms per hash. Login feels instant to a human; brute-forcing a
stolen hash becomes impractical.

bcrypt also salts automatically. Each hash embeds a random salt, so identical
passwords produce different hashes and precomputed rainbow tables are useless.

### Why SHA-256 first

This is the subtle part. **bcrypt silently truncates input at 72 bytes.**

Without pre-hashing, these two passwords are *identical* to bcrypt:

```
"aaaa...(72 chars)...aaaa" + "my-real-secret"
"aaaa...(72 chars)...aaaa" + "totally-different"
```

Both truncate to the same 72 bytes. Pre-hashing with SHA-256 collapses any length
into a fixed 64-character hex digest, so the *entire* password always contributes.

### The exception handler

A malformed hash in the database (corruption, bad migration) should be a **failed
login**, not a 500 error. Never let a data problem become an availability problem
on your auth path.

---

## 14.5 Session tokens

```python
def _hash_token(token: str) -> str:
    """
    Store only a hash of the session token.

    If the database is ever dumped or leaked, the raw tokens are not in it, so
    nobody can resume a live session from a backup.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
```

The reasoning that applies to passwords applies to session tokens.

A session token is a **bearer credential** — whoever holds it *is* that user. If
you store raw tokens and your database leaks, an attacker can resume every live
session immediately.

Storing only `sha256(token)` means:

- The user's cookie holds the raw token
- The database holds only its hash
- Verification hashes the incoming token and compares

A leaked database yields nothing usable.

Plain SHA-256 is correct here, not bcrypt. Tokens are 32 bytes of cryptographic
randomness — there is no dictionary to attack, so the slow hash buys nothing and
would add 250ms to every request.

---

## 14.6 Login

```python
def login(settings: Settings, username: str, password: str) -> tuple[str, User]:
    generic = AuthError("Incorrect username or password.")
    username = (username or "").strip()

    with get_engine(settings).connect() as connection:
        row = connection.execute(
            text(f"SELECT {_USER_COLUMNS}, password_hash "
                 "FROM devbrain_users WHERE lower(username) = lower(:u)"),
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
            text("INSERT INTO devbrain_sessions (token_hash, user_id, expires_at) "
                 "VALUES (:t, :u, :e)"),
            {"t": _hash_token(token), "u": user.id, "e": expires})
        connection.execute(
            text("UPDATE devbrain_users SET last_login_at = now() WHERE id = :i"),
            {"i": user.id})
        connection.execute(text("DELETE FROM devbrain_sessions WHERE expires_at < now()"))

    return token, user
```

### Defence against account enumeration

Two mechanisms, both deliberate:

**Identical error message.** Whether the username does not exist or the password is
wrong, the caller sees *"Incorrect username or password."* Distinguishing them
hands an attacker a way to discover which accounts exist.

**Identical timing.** This is the part people forget. If a missing username returns
instantly while a wrong password takes 250ms of bcrypt work, the *response time*
leaks the answer. So the missing-user branch runs `verify_password` against a dummy
hash to burn comparable time.

The dummy `"$2b$12$" + "." * 53` is a well-formed bcrypt string that no password
matches.

### Token generation

```python
token = secrets.token_urlsafe(32)
```

`secrets`, not `random`. The `random` module is a Mersenne Twister — fast,
reproducible, and **predictable** from prior outputs. Never use it for anything
security-related. `secrets` draws from the OS cryptographic source.

32 bytes ≈ 256 bits of entropy. Unguessable.

### Opportunistic cleanup

```python
connection.execute(text("DELETE FROM devbrain_sessions WHERE expires_at < now()"))
```

Expired sessions are purged on every login. No cron job, no background task — the
table stays small on its own. A pragmatic pattern for low-volume tables.

---

## 14.7 Resolving a request to a user

```python
def user_for_token(settings: Settings, token: str | None) -> User | None:
    if not token:
        return None
    with get_engine(settings).connect() as connection:
        row = connection.execute(
            text(f"""
                SELECT {', '.join('u.' + c for c in _USER_COLUMNS.split(', '))}
                FROM devbrain_sessions s
                JOIN devbrain_users u ON u.id = s.user_id
                WHERE s.token_hash = :t
                  AND s.expires_at > now()
                  AND u.is_active
            """),
            {"t": _hash_token(token)},
        ).first()
    return _row_to_user(row) if row else None
```

**This one query is the entire access-control mechanism.** Three conditions must
all hold:

| Condition | Blocks |
|---|---|
| `token_hash = :t` | Forged or unknown tokens |
| `expires_at > now()` | Expired sessions |
| `u.is_active` | Disabled accounts |

And because it JOINs the user table live, a *deleted* user has no row to join to.
Requirement 5, satisfied by a `JOIN`.

---

## 14.8 The cookie

```python
def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=auth.SESSION_COOKIE,
        value=token,
        max_age=auth.SESSION_TTL_HOURS * 3600,
        httponly=True,      # unreadable from JavaScript
        samesite="lax",
        secure=False,       # DevBrain runs on plain HTTP locally
        path="/",
    )
```

**`httponly=True`** is the important one. JavaScript cannot read an HttpOnly
cookie, so **an XSS bug cannot steal the session**. This is the main reason to
prefer a cookie over `localStorage` — Chapter 13 works hard to prevent XSS, and
this is the defence in depth if that work ever fails.

**`samesite="lax"`** allows normal navigation while blocking cross-site form posts
— basic CSRF protection.

**`secure=False`** is a local-development compromise. Set it to `True` behind HTTPS
so the cookie is never sent over plain HTTP. It is on Chapter 19's checklist.

---

## 14.9 Route protection with dependencies

```python
def current_user(request: Request) -> auth.User:
    settings = _require_settings()
    user = auth.user_for_token(settings, request.cookies.get(auth.SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    return user


def current_admin(request: Request) -> auth.User:
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403,
                            detail="Administrator access is required for this action.")
    return user
```

FastAPI's dependency injection makes protecting a route one parameter:

```python
@app.post("/api/ask", response_model=AskResponse)
def ask(request: AskRequest, user: auth.User = Depends(current_user)) -> AskResponse:
    ...
```

If `current_user` raises, the handler never runs. **Protection is declarative and
impossible to forget halfway through a function.**

Note the status codes: **401** means *not authenticated* ("who are you?"); **403**
means *authenticated but not permitted* ("I know who you are, and no"). Chapter 13
relies on that distinction — it bounces to sign-in on 401 but shows an error on 403.

---

## 14.10 Seeding the administrator

```python
def ensure_admin(settings: Settings) -> str | None:
    username = (os.getenv("ADMIN_USERNAME") or "").strip()
    password = os.getenv("ADMIN_PASSWORD") or ""
    if not username or not password:
        return None

    with get_engine(settings).connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM devbrain_users WHERE lower(username) = lower(:u)"),
            {"u": username}).scalar()
    if exists:
        return None

    create_user(settings, username=username, password=password,
                full_name=..., email=..., is_admin=True)
    return username
```

Three properties:

**Credentials come from configuration**, never source code. The password never
reaches git, and only its bcrypt hash reaches the database.

**It only ever CREATES.** If the account exists, it is left alone — so changing the
password in the app is not silently reverted on the next restart. This is easy to
get wrong: an "upsert" here would make password changes impossible to keep.

**Registration cannot produce an admin:**

```python
user = auth.create_user(..., is_admin=False)   # hardcoded
```

`/api/auth/register` hardcodes `is_admin=False`. **There is no code path from
self-registration to administrator.** Verified by test.

---

## 14.11 Deleting a user

```python
def delete_user(settings: Settings, user_id: int) -> str:
    with get_engine(settings).begin() as connection:
        row = connection.execute(
            text("SELECT username, is_admin FROM devbrain_users WHERE id = :i"),
            {"i": user_id}).first()
        if row is None:
            raise AuthError("That user no longer exists.")

        username, is_admin = str(row[0]), bool(row[1])

        if is_admin:
            remaining = connection.execute(
                text("SELECT count(*) FROM devbrain_users WHERE is_admin AND id <> :i"),
                {"i": user_id}).scalar_one()
            if int(remaining) == 0:
                raise AuthError(
                    "Cannot delete the only administrator — you would lock everyone "
                    "out of user management.")

        connection.execute(text("DELETE FROM devbrain_users WHERE id = :i"), {"i": user_id})
    return username
```

**The last-admin guard** prevents an unrecoverable state. Without it, an admin
could delete themselves and leave the system with no way to manage users. The
endpoint adds a second guard: you cannot delete your own account.

**Documents are kept.** `devbrain_documents.uploaded_by_id` is `ON DELETE SET NULL`
while the `uploaded_by` *name* is a plain column that survives. The knowledge base
should not lose content because someone left, and the audit trail remains.

---

## 14.12 Checkpoint

Verified behaviour from the project's test suite — 38 checks, all passing.

**Anonymous access is blocked:**

```bash
curl -X POST http://localhost:8001/api/ask \
  -H "Content-Type: application/json" -d '{"question":"test question"}'
```
→ `401 {"detail":"Please sign in to continue."}`

**Admin signs in:**

```bash
curl -c cookies.txt -X POST http://localhost:8001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"Shailendra.KS","password":"..."}'
```
→ `200 {"user":{"username":"Shailendra.KS","is_admin":true,...}}`

**Self-registration produces a non-admin:**
→ `200 {"user":{"username":"alice.dev","is_admin":false}}`

**Non-admin blocked from admin routes:** → `403`

**Deleted user loses access immediately:**

| Check | Result |
|---|---|
| Admin deletes alice | `200` |
| Alice's **live session** used again | **`401`** |
| Alice tries to log back in | **`401`** |

That first one is requirement 5, verified. Her session was open and working; the
next request failed.

**Self-protection:** admin deleting own account → `400`. Deleting the only admin →
`400`.

---

## 14.13 Mistakes to avoid

**Storing passwords with a fast hash.** SHA-256, MD5 or anything unsalted is
indefensible. Use bcrypt, argon2 or scrypt.

**Forgetting bcrypt's 72-byte truncation.** Pre-hash.

**Different messages for "no such user" and "wrong password".** Account
enumeration.

**Different *timing* for those two paths.** The subtler version of the same leak.

**Using `random` instead of `secrets`.** Predictable tokens are forgeable tokens.

**Storing raw session tokens.** A leaked backup becomes a set of live sessions.

**`localStorage` for session tokens.** Readable by any XSS. Use an HttpOnly cookie.

**Allowing `is_admin` through registration input.** Even accepting it as an ignored
field invites a future bug. Hardcode it.

**No last-admin guard.** One click from an unrecoverable system.

---

## 14.14 What you have

Authentication with bcrypt-hashed passwords, revocable server-side sessions,
HttpOnly cookies, enumeration-resistant login, declarative route protection, a
seeded administrator that cannot be created any other way, and instant revocation
verified by test.

Next: giving documents owners, and building the admin console.

*Next: [Chapter 15 — Users, Documents and the Admin Console](ch15-users-and-admin.md)*
