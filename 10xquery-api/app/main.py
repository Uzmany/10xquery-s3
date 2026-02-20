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

class SurveyRenameRequest(BaseModel):
    title: str
    description: Optional[str] = None
    userId: str

class SurveyDefinitionRequest(BaseModel):
    userId: str
    definition: dict[str, Any]

class SurveyResponseRequest(BaseModel):
    responderId: Optional[str] = None
    answers: dict[str, Any]

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

@app.post("/v1/surveys", tags=["surveys"])
def create_survey(body: SurveyCreateRequest):
    survey_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    title = (body.title or "Untitled Survey").strip()
    desc = body.description or ""
    owner_id = body.userId

    table.put_item(Item={
        "PK": _pk(survey_id),
        "SK": "META",
        "title": title,
        "description": desc,
        "createdAt": created_at,
        "lastUpdatedAt": created_at,
        "ownerId": owner_id,
        "status": "draft",
        "GSI1PK": f"USER#{owner_id}",
        "GSI1SK": created_at,
    })
    return {"surveyId": survey_id, "title": title, "createdAt": created_at, "ownerId": owner_id}

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
            "createdAt": i.get("createdAt"),
            "lastUpdatedAt": i.get("lastUpdatedAt"),
        })
    return results

@app.get("/v1/surveys/{survey_id}", tags=["surveys"])
def get_survey(survey_id: str, userId: Optional[str] = None):
    # If userId is passed, verify ownership, else just return public info (for taking survey)
    resp = table.get_item(Key={"PK": _pk(survey_id), "SK": "META"})
    item = resp.get("Item")
    if not item:
        raise HTTPException(404, "Survey not found")
        
    return {
        "surveyId": survey_id,
        "title": item.get("title"),
        "description": item.get("description"),
        "status": item.get("status"),
        "createdAt": item.get("createdAt")
    }

@app.put("/v1/surveys/{survey_id}/meta", tags=["surveys"])
def update_survey_meta(survey_id: str, req: SurveyRenameRequest):
    _ensure_owner(survey_id, req.userId)
    table.update_item(
        Key={"PK": _pk(survey_id), "SK": "META"},
        UpdateExpression="SET #t = :t, #d = :d, #lu = :lu, #gsi = :lu",
        ExpressionAttributeNames={"#t": "title", "#d": "description", "#lu": "lastUpdatedAt", "#gsi": "GSI1SK"},
        ExpressionAttributeValues={":t": req.title, ":d": req.description or "", ":lu": datetime.now(timezone.utc).isoformat()},
    )
    return {"ok": True}

@app.get("/v1/surveys/{survey_id}/definition", tags=["surveys"])
def get_survey_definition(survey_id: str):
    # Public endpoint to get questions
    resp = table.get_item(Key={"PK": _pk(survey_id), "SK": "DEFINITION"})
    item = resp.get("Item")
    if not item:
        return {"questions": []}
    return jsonable_encoder(item.get("json"))

@app.put("/v1/surveys/{survey_id}/definition", tags=["surveys"])
def put_survey_definition(survey_id: str, body: SurveyDefinitionRequest):
    _ensure_owner(survey_id, body.userId)
    
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
    # Verify survey exists
    resp = table.get_item(Key={"PK": _pk(survey_id), "SK": "META"})
    if not resp.get("Item"):
        raise HTTPException(404, "Survey not found")

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
        "ip": ip,
        "ts": ts
    })
    
    return {"ok": True, "responseId": resp_id}

@app.get("/v1/surveys/{survey_id}/responses", tags=["surveys"])
def list_responses(survey_id: str, userId: str):
    _ensure_owner(survey_id, userId)
    resp = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
        ExpressionAttributeValues={":pk": _pk(survey_id), ":p": "RESP#"}
    )
    items = resp.get("Items", [])
    
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
            "answers": _decimal_to_native(i.get("answers", {}))
        }
        for i in sorted(items, key=lambda x: x.get("ts", ""))
    ]

@app.delete("/v1/surveys/{survey_id}", tags=["surveys"])
def delete_survey(survey_id: str, userId: str):
    _ensure_owner(survey_id, userId)
    # Batch delete
    resp = table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": _pk(survey_id)},
        ProjectionExpression="PK, SK",
    )
    items = resp.get("Items", [])
    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return {"ok": True}

