import os
import uuid
import time
import base64
import secrets
import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, Dict

import boto3
import httpx
import jwt
from boto3.dynamodb.conditions import Key
from fastapi import APIRouter, HTTPException, Request, Response, Depends, Cookie, Header
from pydantic import BaseModel, Field, EmailStr
from argon2 import PasswordHasher


router = APIRouter(tags=["auth", "users", "sessions"])


# Environment & configuration
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DDB_ENDPOINT = os.environ.get("DDB_ENDPOINT") or os.environ.get("DDB_ENDPOINT_URL")
USER_PROFILES_TABLE = os.environ.get("USER_PROFILES_TABLE", "10xquery_UserProfiles")
USER_SESSIONS_TABLE = os.environ.get("USER_SESSIONS_TABLE", "10xquery_UserSessions")

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
JWT_ISSUER = os.environ.get("JWT_ISSUER", "10xquery-api")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "10xquery-client")

ACCESS_TOKEN_TTL_MIN = int(os.environ.get("ACCESS_TOKEN_TTL_MIN", "10"))
REFRESH_TOKEN_TTL_DAYS = int(os.environ.get("REFRESH_TOKEN_TTL_DAYS", "14"))

SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "refresh_token")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
COOKIE_SAMESITE = os.environ.get("COOKIE_SAMESITE", "none").lower()
COOKIE_PATH = os.environ.get("COOKIE_PATH", "/")

# Default Google Client ID for production (safe to hardcode per user's instruction)
GOOGLE_CLIENT_ID = os.environ.get(
    "GOOGLE_CLIENT_ID",
    "845054533001-8sfjtgmogkr6osa73jiep360c02farnl.apps.googleusercontent.com",
)

REFRESH_TOKEN_PEPPER = os.environ.get("REFRESH_TOKEN_PEPPER", JWT_SECRET)


# DynamoDB client
if DDB_ENDPOINT:
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=AWS_REGION,
        endpoint_url=DDB_ENDPOINT,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
else:
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)

profiles_table = dynamodb.Table(USER_PROFILES_TABLE)
sessions_table = dynamodb.Table(USER_SESSIONS_TABLE)


# Password hashing
password_hasher = PasswordHasher()


# Models
class Profile(BaseModel):
    userId: str
    email: EmailStr
    displayName: Optional[str] = None
    avatarUrl: Optional[str] = None
    preferences: Optional[Dict[str, Any]] = None
    createdAt: str
    updatedAt: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class LoginResponse(BaseModel):
    accessToken: str
    user: Profile


class SignUpRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    displayName: Optional[str] = None


class RefreshResponse(BaseModel):
    accessToken: str


class PatchProfileRequest(BaseModel):
    displayName: Optional[str] = None
    avatarUrl: Optional[str] = None
    preferences: Optional[Dict[str, Any]] = None


class ChangePasswordRequest(BaseModel):
    oldPassword: str = Field(min_length=6)
    newPassword: str = Field(min_length=8)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    newPassword: str = Field(min_length=8)


class GoogleVerifyRequest(BaseModel):
    idToken: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl_epoch(days: int) -> int:
    return int(time.time() + days * 24 * 60 * 60)


def _generate_access_token(user_id: str, email: str, display_name: Optional[str]) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=ACCESS_TOKEN_TTL_MIN)
    payload = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "sub": user_id,
        "email": email,
        "name": display_name,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _generate_refresh_token(session_id: str) -> str:
    rnd = secrets.token_urlsafe(48)
    return f"{session_id}.{rnd}"


def _hash_refresh_token(token: str) -> str:
    data = (REFRESH_TOKEN_PEPPER + token).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _parse_session_id_from_token(token: str) -> Optional[str]:
    if not token or "." not in token:
        return None
    return token.split(".", 1)[0]


def _sanitize_profile(item: Dict[str, Any]) -> Profile:
    return Profile(
        userId=item["userId"],
        email=item["email"],
        displayName=item.get("displayName"),
        avatarUrl=item.get("avatarUrl"),
        preferences=item.get("preferences"),
        createdAt=item.get("createdAt", ""),
        updatedAt=item.get("updatedAt", ""),
    )


def _get_profile_by_user_id(user_id: str) -> Optional[Dict[str, Any]]:
    resp = profiles_table.get_item(Key={"userId": user_id})
    return resp.get("Item")


def _get_profile_by_email(email: str) -> Optional[Dict[str, Any]]:
    resp = profiles_table.query(
        IndexName="email-index",
        KeyConditionExpression=Key("email").eq(email),
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _get_profile_by_identity(identity_key: str) -> Optional[Dict[str, Any]]:
    resp = profiles_table.query(
        IndexName="identityKey-index",
        KeyConditionExpression=Key("identityKey").eq(identity_key),
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _put_profile(item: Dict[str, Any]) -> None:
    profiles_table.put_item(Item=item)


def _set_refresh_cookie(response: Response, token: str, days: int) -> None:
    max_age = days * 24 * 60 * 60
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path=COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value="",
        max_age=0,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path=COOKIE_PATH,
    )


def _client_metadata(request: Request) -> Dict[str, Any]:
    return {
        "ip": (request.client.host if request.client else None),
        "userAgent": request.headers.get("user-agent"),
    }


def _create_session(user_id: str, response: Response, request: Request) -> str:
    session_id = str(uuid.uuid4())
    refresh_token = _generate_refresh_token(session_id)
    refresh_hash = _hash_refresh_token(refresh_token)
    now = _now_iso()
    ttl = _ttl_epoch(REFRESH_TOKEN_TTL_DAYS)
    meta = _client_metadata(request)
    sessions_table.put_item(Item={
        "sessionId": session_id,
        "userId": user_id,
        "refreshHash": refresh_hash,
        "createdAt": now,
        "lastUsedAt": now,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_TTL_DAYS)).isoformat(),
        "ip": meta["ip"],
        "userAgent": meta["userAgent"],
        "ttl": ttl,
    })
    _set_refresh_cookie(response, refresh_token, REFRESH_TOKEN_TTL_DAYS)
    return session_id


def _rotate_session(refresh_token: str, response: Response) -> Dict[str, Any]:
    session_id = _parse_session_id_from_token(refresh_token)
    if not session_id:
        raise HTTPException(401, "Invalid refresh token")

    resp = sessions_table.get_item(Key={"sessionId": session_id})
    sess = resp.get("Item")
    if not sess:
        raise HTTPException(401, "Session not found")

    expected_hash = sess.get("refreshHash")
    if _hash_refresh_token(refresh_token) != expected_hash:
        # Reuse detected; revoke session
        sessions_table.delete_item(Key={"sessionId": session_id})
        _clear_refresh_cookie(response)
        raise HTTPException(401, "Refresh token mismatch")

    # Rotate
    new_token = _generate_refresh_token(session_id)
    new_hash = _hash_refresh_token(new_token)
    now = _now_iso()
    ttl = _ttl_epoch(REFRESH_TOKEN_TTL_DAYS)
    sessions_table.update_item(
        Key={"sessionId": session_id},
        UpdateExpression="SET refreshHash=:rh, lastUsedAt=:lu, ttl=:ttl",
        ExpressionAttributeValues={":rh": new_hash, ":lu": now, ":ttl": ttl},
    )
    _set_refresh_cookie(response, new_token, REFRESH_TOKEN_TTL_DAYS)
    return sess


def _decode_access_token_from_header(authorization: Optional[str]) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Access token expired")
    except Exception:
        raise HTTPException(401, "Invalid access token")


def _require_current_user(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    return _decode_access_token_from_header(authorization)


@router.post("/users", response_model=LoginResponse)
def create_user(req: SignUpRequest, request: Request, response: Response):
    existing = _get_profile_by_email(req.email)
    if existing:
        raise HTTPException(409, "Email already in use")

    # Generate a unique 8-digit user number
    user_id = str(random.randint(10000000, 99999999))
    while _get_profile_by_user_id(user_id):
        user_id = str(random.randint(10000000, 99999999))

    now = _now_iso()
    display_name = req.displayName or req.email.split("@")[0]
    try:
        pwd_hash = password_hasher.hash(req.password)
    except Exception:
        raise HTTPException(400, "Invalid password")
    item = {
        "userId": user_id,
        "email": req.email,
        "displayName": display_name,
        "avatarUrl": None,
        "preferences": {},
        "createdAt": now,
        "updatedAt": now,
        "passwordHash": pwd_hash,
    }
    _put_profile(item)

    _create_session(user_id, response, request)
    access = _generate_access_token(user_id, req.email, display_name)
    return {"accessToken": access, "user": _sanitize_profile(item)}


@router.post("/auth/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request, response: Response):
    profile = _get_profile_by_email(req.email)
    if not profile or not profile.get("passwordHash"):
        raise HTTPException(401, "Invalid credentials")
    try:
        password_hasher.verify(profile["passwordHash"], req.password)
    except Exception:
        raise HTTPException(401, "Invalid credentials")

    user_id = profile["userId"]
    _create_session(user_id, response, request)
    access = _generate_access_token(user_id, profile["email"], profile.get("displayName"))
    return {"accessToken": access, "user": _sanitize_profile(profile)}


@router.post("/auth/logout")
def logout(response: Response, refresh_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME)):
    if refresh_token:
        session_id = _parse_session_id_from_token(refresh_token)
        if session_id:
            sessions_table.delete_item(Key={"sessionId": session_id})
    _clear_refresh_cookie(response)
    return {"ok": True}


@router.post("/auth/refresh", response_model=RefreshResponse)
def refresh_token(response: Response, refresh_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME)):
    if not refresh_token:
        raise HTTPException(401, "Refresh token required")
    sess = _rotate_session(refresh_token, response)
    user = _get_profile_by_user_id(sess["userId"]) or {}
    access = _generate_access_token(sess["userId"], user.get("email", ""), user.get("displayName"))
    return {"accessToken": access}


@router.get("/session", response_model=Profile)
def session_info(user_claims: Dict[str, Any] = Depends(_require_current_user)):
    user = _get_profile_by_user_id(user_claims["sub"]) or {}
    if not user:
        raise HTTPException(404, "User not found")
    return _sanitize_profile(user)


@router.get("/users/me", response_model=Profile)
def get_me(user_claims: Dict[str, Any] = Depends(_require_current_user)):
    user = _get_profile_by_user_id(user_claims["sub"]) or {}
    if not user:
        raise HTTPException(404, "User not found")
    return _sanitize_profile(user)


@router.patch("/users/me")
def update_me(body: PatchProfileRequest, user_claims: Dict[str, Any] = Depends(_require_current_user)):
    user_id = user_claims["sub"]
    update_expr = []
    expr_names: Dict[str, str] = {}
    expr_vals: Dict[str, Any] = {":updatedAt": _now_iso()}
    if body.displayName is not None:
        update_expr.append("#dn=:dn")
        expr_names["#dn"] = "displayName"
        expr_vals[":dn"] = body.displayName
    if body.avatarUrl is not None:
        update_expr.append("#au=:au")
        expr_names["#au"] = "avatarUrl"
        expr_vals[":au"] = body.avatarUrl
    if body.preferences is not None:
        update_expr.append("#pf=:pf")
        expr_names["#pf"] = "preferences"
        expr_vals[":pf"] = body.preferences
    update_expr.append("updatedAt=:updatedAt")
    if len(update_expr) == 1:
        user = _get_profile_by_user_id(user_id)
        if not user:
            raise HTTPException(404, "User not found")
        return _sanitize_profile(user)
    profiles_table.update_item(
        Key={"userId": user_id},
        UpdateExpression="SET " + ", ".join(update_expr),
        ExpressionAttributeNames=expr_names or None,
        ExpressionAttributeValues=expr_vals,
    )
    user = _get_profile_by_user_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    return _sanitize_profile(user)


@router.put("/users/me/password")
def change_password(body: ChangePasswordRequest, user_claims: Dict[str, Any] = Depends(_require_current_user)):
    user = _get_profile_by_user_id(user_claims["sub"]) or {}
    if not user or not user.get("passwordHash"):
        raise HTTPException(400, "Password not set for this account")
    try:
        password_hasher.verify(user["passwordHash"], body.oldPassword)
    except Exception:
        raise HTTPException(401, "Invalid credentials")
    new_hash = password_hasher.hash(body.newPassword)
    profiles_table.update_item(
        Key={"userId": user_claims["sub"]},
        UpdateExpression="SET passwordHash=:ph, updatedAt=:ua",
        ExpressionAttributeValues={":ph": new_hash, ":ua": _now_iso()},
    )
    return {"ok": True}


@router.post("/auth/forgot-password")
def forgot_password(body: ForgotPasswordRequest):
    user = _get_profile_by_email(body.email)
    if not user:
        # Do not leak
        return {"ok": True}
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=20)
    payload = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "sub": user["userId"],
        "email": user["email"],
        "type": "pwd-reset",
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    # In production, send via email; for development, return token
    return {"ok": True, "resetToken": token}


@router.post("/auth/reset-password")
def reset_password(body: ResetPasswordRequest):
    try:
        payload = jwt.decode(
            body.token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(400, "Reset token expired")
    except Exception:
        raise HTTPException(400, "Invalid reset token")
    if payload.get("type") != "pwd-reset":
        raise HTTPException(400, "Invalid reset token type")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(400, "Invalid reset token")
    new_hash = password_hasher.hash(body.newPassword)
    profiles_table.update_item(
        Key={"userId": user_id},
        UpdateExpression="SET passwordHash=:ph, updatedAt=:ua",
        ExpressionAttributeValues={":ph": new_hash, ":ua": _now_iso()},
    )
    return {"ok": True}


@router.post("/auth/google/verify", response_model=LoginResponse)
def google_verify(req: GoogleVerifyRequest, request: Request, response: Response):
    # Verify the Google ID token using tokeninfo endpoint
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            r = client.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": req.idToken})
            data = r.json()
    except Exception:
        raise HTTPException(401, "Unable to verify Google token")
    if r.status_code != 200:
        raise HTTPException(401, "Invalid Google token")

    aud = data.get("aud")
    iss = data.get("iss")
    sub = data.get("sub")
    email = data.get("email")
    name = data.get("name")
    picture = data.get("picture")
    if GOOGLE_CLIENT_ID and aud != GOOGLE_CLIENT_ID:
        raise HTTPException(401, "Google token audience mismatch")
    if iss not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(401, "Google token issuer mismatch")
    if not sub or not email:
        raise HTTPException(401, "Google token missing claims")

    identity_key = f"google#{sub}"
    profile = _get_profile_by_identity(identity_key)
    if not profile:
        profile = _get_profile_by_email(email)
        if profile:
            # Link identity
            profiles_table.update_item(
                Key={"userId": profile["userId"]},
                UpdateExpression="SET identityKey=:ik, updatedAt=:ua",
                ExpressionAttributeValues={":ik": identity_key, ":ua": _now_iso()},
            )
            profile = _get_profile_by_user_id(profile["userId"]) or {}
        else:
            # Create new profile
            user_id = str(random.randint(10000000, 99999999))
            while _get_profile_by_user_id(user_id):
                user_id = str(random.randint(10000000, 99999999))
                
            now = _now_iso()
            item = {
                "userId": user_id,
                "email": email,
                "displayName": name,
                "avatarUrl": picture,
                "preferences": {},
                "identityKey": identity_key,
                "createdAt": now,
                "updatedAt": now,
            }
            _put_profile(item)
            profile = item

    user_id = profile["userId"]
    _create_session(user_id, response, request)
    access = _generate_access_token(user_id, profile["email"], profile.get("displayName"))
    return {"accessToken": access, "user": _sanitize_profile(profile)}



