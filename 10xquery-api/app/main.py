import os
import time
import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Any
from decimal import Decimal

import boto3
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field
from . import users as users

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("DDB_TABLE", "10xquery_surveys")
DDB_ENDPOINT = os.environ.get("DDB_ENDPOINT") or os.environ.get("DDB_ENDPOINT_URL")

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

table = dynamodb.Table(TABLE_NAME)

app = FastAPI(title="10xQuery API", version="0.1.0")

# Use explicit origins by default so browsers allow credentials (cookies)
_cors_default = "https://10xquery.com,https://www.10xquery.com,http://localhost:5173,http://localhost:3000,http://127.0.0.1:5500,http://localhost:5500"
_cors_env = os.environ.get("CORS_ORIGINS", _cors_default)
ALLOWED_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Mount user/auth routes
app.include_router(users.router, prefix="/v1")

@app.get("/", tags=["health"])
def health():
    return {"status": "ok"}


# Data models
class SurveyCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    userId: str
    visibility: Optional[str] = "public"
    allowedUsers: Optional[List[str]] = []

class SurveyRenameRequest(BaseModel):
    title: str
    description: Optional[str] = None
    userId: str
    visibility: Optional[str] = "public"
    allowedUsers: Optional[List[str]] = []

class CollaboratorsRequest(BaseModel):
    userId: str          # must be the owner
    collaborators: List[str]  # full replacement list of collaborator userIds

class SurveyDefinitionRequest(BaseModel):
    userId: str
    definition: dict[str, Any]

class SurveyResponseRequest(BaseModel):
    responderId: Optional[str] = None
    answers: dict[str, Any]
    rowKeys: Optional[List[str]] = None

class UpdateResponseRequest(BaseModel):
    userId: str
    answers: dict[str, Any]
    rowKeys: Optional[List[str]] = None

def _pk(survey_id: str) -> str:
    return f"SRV#{survey_id}"

def _ensure_owner(survey_id: str, user_id: str):
    """Raises 404 if survey missing, 403 if user does not own it."""
    resp = table.get_item(Key={"PK": _pk(survey_id), "SK": "META"})
    item = resp.get("Item")
    if not item:
        raise HTTPException(404, "Survey not found")
    if item.get("ownerId") != user_id:
        raise HTTPException(403, "Forbidden")
    return item

def _ensure_editor(survey_id: str, user_id: str):
    """Owner OR collaborator can perform editor actions."""
    resp = table.get_item(Key={"PK": _pk(survey_id), "SK": "META"})
    item = resp.get("Item")
    if not item:
        raise HTTPException(404, "Survey not found")
    if item.get("ownerId") != user_id and user_id not in item.get("collaborators", []):
        raise HTTPException(403, "Forbidden")
    return item

def _check_survey_access(survey_id: str, user_id: Optional[str] = None):
    resp = table.get_item(Key={"PK": _pk(survey_id), "SK": "META"})
    item = resp.get("Item")
    if not item:
        raise HTTPException(404, "Survey not found")
        
    visibility = item.get("visibility", "public")
    allowed_users = item.get("allowedUsers", [])
    owner_id = item.get("ownerId")
    
    if visibility == "private":
        if not user_id or (user_id != owner_id and user_id not in allowed_users):
            raise HTTPException(403, "This survey is private. You do not have access.")
            
    return item

@app.post("/v1/surveys", tags=["surveys"])
def create_survey(body: SurveyCreateRequest):
    survey_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    title = (body.title or "Untitled Survey").strip()
    desc = body.description or ""
    owner_id = body.userId
    visibility = body.visibility or "public"
    allowed_users = body.allowedUsers or []

    table.put_item(Item={
        "PK": _pk(survey_id),
        "SK": "META",
        "title": title,
        "description": desc,
        "createdAt": created_at,
        "lastUpdatedAt": created_at,
        "ownerId": owner_id,
        "status": "draft",
        "visibility": visibility,
        "allowedUsers": allowed_users,
        "collaborators": [],
        "responseCount": 0,
        "GSI1PK": f"USER#{owner_id}",
        "GSI1SK": created_at,
    })
    return {"surveyId": survey_id, "title": title, "createdAt": created_at, "ownerId": owner_id, "visibility": visibility}

@app.get("/v1/surveys", tags=["surveys"])
def list_surveys(userId: str):
    # Query GSI by user to fetch surveys ordered by lastUpdatedAt desc
    resp = table.query(
        IndexName="UserConversations",
        KeyConditionExpression="GSI1PK = :u",
        ExpressionAttributeValues={":u": f"USER#{userId}"},
        ScanIndexForward=False,
    )
    items = resp.get("Items", [])
    results = []
    for i in items:
        results.append({
            "surveyId": i["PK"].split("#", 1)[1],
            "title": i.get("title"),
            "description": i.get("description"),
            "status": i.get("status"),
            "ownerId": i.get("ownerId"),
            "visibility": i.get("visibility", "public"),
            "createdAt": i.get("createdAt"),
            "lastUpdatedAt": i.get("lastUpdatedAt"),
            "collaborators": i.get("collaborators", []),
            "responseCount": int(i.get("responseCount", 0)),
        })
    return results

@app.get("/v1/surveys/{survey_id}", tags=["surveys"])
def get_survey(survey_id: str, userId: Optional[str] = None):
    # If userId is passed, verify ownership, else just return public info (for taking survey)
    item = _check_survey_access(survey_id, userId)
        
    return {
        "surveyId": survey_id,
        "title": item.get("title"),
        "description": item.get("description"),
        "status": item.get("status"),
        "visibility": item.get("visibility", "public"),
        "allowedUsers": item.get("allowedUsers", []),
        "createdAt": item.get("createdAt")
    }

@app.put("/v1/surveys/{survey_id}/meta", tags=["surveys"])
def update_survey_meta(survey_id: str, req: SurveyRenameRequest):
    _ensure_owner(survey_id, req.userId)
    table.update_item(
        Key={"PK": _pk(survey_id), "SK": "META"},
        UpdateExpression="SET #t = :t, #d = :d, #lu = :lu, #gsi = :lu, visibility = :v, allowedUsers = :au",
        ExpressionAttributeNames={"#t": "title", "#d": "description", "#lu": "lastUpdatedAt", "#gsi": "GSI1SK"},
        ExpressionAttributeValues={
            ":t": req.title, 
            ":d": req.description or "", 
            ":lu": datetime.now(timezone.utc).isoformat(),
            ":v": req.visibility or "public",
            ":au": req.allowedUsers or []
        },
    )
    return {"ok": True}

@app.get("/v1/surveys/{survey_id}/definition", tags=["surveys"])
def get_survey_definition(survey_id: str, userId: Optional[str] = None):
    # Editors (owner/collaborator) always get access.
    # Everyone else goes through the standard access check (respects private/allowedUsers).
    resp = table.get_item(Key={"PK": _pk(survey_id), "SK": "META"})
    meta = resp.get("Item")
    if not meta:
        raise HTTPException(404, "Survey not found")
    is_editor = userId and (
        meta.get("ownerId") == userId or userId in meta.get("collaborators", [])
    )
    if not is_editor:
        _check_survey_access(survey_id, userId)
    
    resp = table.get_item(Key={"PK": _pk(survey_id), "SK": "DEFINITION"})
    item = resp.get("Item")
    if not item:
        return {"questions": []}
    return jsonable_encoder(item.get("json"))

@app.put("/v1/surveys/{survey_id}/definition", tags=["surveys"])
def put_survey_definition(survey_id: str, body: SurveyDefinitionRequest):
    _ensure_editor(survey_id, body.userId)
    
    def to_ddb_numbers(obj):
        if isinstance(obj, float):
            return Decimal(str(obj))
        if isinstance(obj, list):
            return [to_ddb_numbers(v) for v in obj]
        if isinstance(obj, dict):
            return {k: to_ddb_numbers(v) for k, v in obj.items()}
        return obj

    ddb_json = to_ddb_numbers(body.definition)
    
    table.put_item(Item={
        "PK": _pk(survey_id),
        "SK": "DEFINITION",
        "json": ddb_json,
        "updatedAt": datetime.now(timezone.utc).isoformat()
    })
    
    table.update_item(
        Key={"PK": _pk(survey_id), "SK": "META"},
        UpdateExpression="SET #lu=:lu, #gsi=:lu",
        ExpressionAttributeNames={"#lu": "lastUpdatedAt", "#gsi": "GSI1SK"},
        ExpressionAttributeValues={":lu": datetime.now(timezone.utc).isoformat()},
    )
    
    return {"ok": True}

@app.post("/v1/surveys/{survey_id}/responses", tags=["surveys"])
def submit_response(survey_id: str, req: Request, body: SurveyResponseRequest):
    # Verify access
    _check_survey_access(survey_id, body.responderId)

    resp_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    ip = req.client.host if req.client else "unknown"
    
    def to_ddb_numbers(obj):
        if isinstance(obj, float):
            return Decimal(str(obj))
        if isinstance(obj, list):
            return [to_ddb_numbers(v) for v in obj]
        if isinstance(obj, dict):
            return {k: to_ddb_numbers(v) for k, v in obj.items()}
        return obj

    ddb_answers = to_ddb_numbers(body.answers)

    table.put_item(Item={
        "PK": _pk(survey_id),
        "SK": f"RESP#{ts}#{resp_id}",
        "responderId": body.responderId,
        "answers": ddb_answers,
        "rowKeys": body.rowKeys or [],
        "ip": ip,
        "ts": ts
    })
    # Atomically increment the response counter on the survey META
    table.update_item(
        Key={"PK": _pk(survey_id), "SK": "META"},
        UpdateExpression="SET responseCount = if_not_exists(responseCount, :zero) + :inc",
        ExpressionAttributeValues={":zero": 0, ":inc": 1},
    )

    return {"ok": True, "responseId": resp_id}

@app.get("/v1/surveys/{survey_id}/responses", tags=["surveys"])
def list_responses(survey_id: str, userId: str):
    _ensure_editor(survey_id, userId)
    # Paginate through all pages to avoid silent 1MB truncation
    items = []
    kwargs = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :p)",
        "ExpressionAttributeValues": {":pk": _pk(survey_id), ":p": "RESP#"},
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    
    # Simple conversion
    def _decimal_to_native(obj):
        if isinstance(obj, Decimal):
            if obj == int(obj):
                return int(obj)
            return float(obj)
        if isinstance(obj, list):
            return [_decimal_to_native(v) for v in obj]
        if isinstance(obj, dict):
            return {k: _decimal_to_native(v) for k, v in obj.items()}
        return obj

    return [
        {
            "responseId": i["SK"].split("#")[-1],
            "ts": i.get("ts"),
            "responderId": i.get("responderId"),
            "answers": _decimal_to_native(i.get("answers", {})),
            "rowKeys": i.get("rowKeys", [])
        }
        for i in sorted(items, key=lambda x: x.get("ts", ""))
    ]

@app.put("/v1/surveys/{survey_id}/responses/{response_id}", tags=["surveys"])
def update_response(survey_id: str, response_id: str, body: UpdateResponseRequest):
    _ensure_editor(survey_id, body.userId)

    resp = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
        ExpressionAttributeValues={":pk": _pk(survey_id), ":p": "RESP#"}
    )
    item = next((i for i in resp.get("Items", []) if i["SK"].endswith(response_id)), None)
    if not item:
        raise HTTPException(404, "Response not found")

    def to_ddb(obj):
        if isinstance(obj, float): return Decimal(str(obj))
        if isinstance(obj, list):  return [to_ddb(v) for v in obj]
        if isinstance(obj, dict):  return {k: to_ddb(v) for k, v in obj.items()}
        return obj

    table.update_item(
        Key={"PK": item["PK"], "SK": item["SK"]},
        UpdateExpression="SET answers = :a, rowKeys = :rk",
        ExpressionAttributeValues={
            ":a":  to_ddb(body.answers),
            ":rk": body.rowKeys if body.rowKeys is not None else item.get("rowKeys", [])
        }
    )
    return {"ok": True}

@app.put("/v1/surveys/{survey_id}/collaborators", tags=["surveys"])
def update_collaborators(survey_id: str, body: CollaboratorsRequest):
    _ensure_owner(survey_id, body.userId)
    # Prevent owner from adding themselves
    collabs = [c for c in body.collaborators if c != body.userId]
    table.update_item(
        Key={"PK": _pk(survey_id), "SK": "META"},
        UpdateExpression="SET collaborators = :c",
        ExpressionAttributeValues={":c": collabs},
    )
    return {"ok": True, "collaborators": collabs}

@app.get("/v1/users/lookup/{user_id}", tags=["users"])
def lookup_user(user_id: str):
    """Public profile lookup — returns only non-sensitive fields."""
    from .users import _get_profile_by_user_id
    profile = _get_profile_by_user_id(user_id)
    if not profile:
        raise HTTPException(404, "User not found")
    return {
        "userId": profile["userId"],
        "displayName": profile.get("displayName"),
    }



@app.delete("/v1/surveys/{survey_id}", tags=["surveys"])
def delete_survey(survey_id: str, userId: str):
    _ensure_owner(survey_id, userId)
    # Paginate to catch all items (avoids silent 1MB truncation)
    all_items = []
    kwargs = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": _pk(survey_id)},
        "ProjectionExpression": "PK, SK",
    }
    while True:
        resp = table.query(**kwargs)
        all_items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    with table.batch_writer() as batch:
        for item in all_items:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return {"ok": True}

