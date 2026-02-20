# 10xQuery — Architecture Document

> **Last updated:** February 20, 2026
> **Product:** Minimalist, high-performance survey and polling tool
> **Domain:** https://10xquery.com / https://www.10xquery.com
> **API:** https://api.10xquery.com
> **AWS Account:** 912112639269 · Region: us-east-1

---

## 1. High-Level Overview

10xQuery is a sleek, modern polling tool. Users can create public or private surveys and share the survey links. The infrastructure is serverless-hybrid utilizing static hosting and an EC2-backed FastAPI backend.

```text
┌─────────────────────────────────────────────────────────────────┐
│                          USERS / BROWSER                        │
│                (index.html / survey.html)                       │
└────────────────────────────┬────────────────────────────────────┘
                             │  HTTPS
                             ▼
              ┌──────────────────────────────┐
              │  CloudFront (CDN)            │
              │  10xquery.com  → E2BTNOZGJYMK2F│
              └──────────┬───────────────────┘
                         │  Origin: S3 Website Hosting
                         ▼
              ┌──────────────────────────────┐
              │  S3 Bucket: www.10xquery.com │
              │  (static website hosting)    │
              └──────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  API Calls (from browser)                                       │
│  https://api.10xquery.com/*                                     │
└────────────────────────────┬────────────────────────────────────┘
                             │  HTTPS (Route 53 → ALB)
                             ▼
              ┌──────────────────────────────┐
              │  Application Load Balancer   │
              │  ketzek-lb                   │
              │  Rule: Host is api.10xquery.com                   │
              └──────────┬───────────────────┘
                         │  HTTP :8000 → Target Group
                         ▼
              ┌──────────────────────────────┐
              │  EC2: t2.medium              │
              │  IP: 54.242.99.16            │
              │  uvicorn + FastAPI (:8000)   │
              └──────────┬───────────────────┘
                         │
              ┌──────────▼───────────────────┐
              │  DynamoDB (3 tables)         │
              │  • 10xquery_surveys          │
              │  • 10xquery_UserProfiles     │
              │  • 10xquery_UserSessions     │
              └──────────────────────────────┘
```

---

## 2. Infrastructure & DynamoDB Schema

### 2.1 DynamoDB Tables
All tables run on **PAY_PER_REQUEST** (on-demand) billing.

1. **`10xquery_surveys`** (Single-Table Design)
   - **Primary Key:** `PK` (String, HASH) + `SK` (String, RANGE)
   - **GSI:** `UserConversations` (GSI1PK, GSI1SK) for listing user's surveys.
   - **Schema Details:**
     - `META`: Stores `title`, `description`, `ownerId`, `visibility` ("public" or "private"), `allowedUsers` (list of strings).
     - `DEFINITION`: Stores the survey questions JSON.
     - `RESP#{timestamp}#{uuid}`: Stores participant answers.

2. **`10xquery_UserProfiles`**
   - **Primary Key:** `userId` (String, HASH) - **Unique 8-digit numeric string**.
   - **GSIs:** `email-index`, `identityKey-index`.
   - **Fields:** `email`, `displayName`, `passwordHash`.

3. **`10xquery_UserSessions`**
   - **Primary Key:** `sessionId` (String, HASH)
   - **GSI:** `userId-index`
   - **Fields:** `refreshHash`, `expiresAt`, `ttl`.

### 2.2 Access Control & Private Sharing
Instead of UUIDs, users receive a unique **8-digit User ID**. 
When a survey is marked as `private`, its `allowedUsers` field is populated with 8-digit IDs. The FastAPI backend verifies the requesting user's ID against the `allowedUsers` list before returning the survey `DEFINITION` or `META`.

---

## 3. Frontend Architecture

- **UI Framework:** Vanilla JavaScript with HTML5. No build steps required.
- **Styling:** Tailwind CSS (CDN) combined with custom sleek dark/light aesthetics.
- **Deployment:** Git push is synced to an S3 bucket and CloudFront cache is invalidated automatically by the `push-and-publish` bash script.

### 3.1 Pages
| Page | Purpose |
|------|---------|
| `index.html` | Authentication (Login/Signup), Dashboard, Survey Creation, ID Sharing. |
| `survey.html` | Public facing survey submission page. Handles private survey unlock prompts. |

---

## 4. Backend Architecture (10xquery-api)

- **Framework:** FastAPI (Python 3.9+)
- **Server:** Uvicorn running on port `8000`.
- **Database driver:** `boto3`

### 4.1 Deployment
Deployment is entirely scripted. Executing `./push-and-publish "commit msg"` does the following:
1. Pushes the source to `github.com/Uzmany/10xquery-s3`.
2. Syncs frontend static assets to `s3://www.10xquery.com`.
3. Issues a CloudFront invalidation request.
4. SSHs into the backend EC2 server, pulls the repository, kills the existing Uvicorn process, and runs a new instance on port `8000` via `nohup`.

