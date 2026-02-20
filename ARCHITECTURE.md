# 10xQuery — Full Architecture Document

> **Last updated:** February 20, 2026
> **Product:** Minimalist, high-performance survey, polling, and data collection tool
> **Domain:** https://10xquery.com / https://www.10xquery.com
> **API:** https://api.10xquery.com
> **AWS Account:** 912112639269 · Region: us-east-1
> **GitHub Repository:** https://github.com/Uzmany/10xquery-s3

---

## 1. High-Level Overview

10xQuery is a sleek, modern polling tool. Users can create public or private surveys, define complex question types (text, long-form, single-choice, multiple-choice), and share survey links to collect participant responses. Results can be viewed in a comprehensive dashboard.

The infrastructure is a serverless-hybrid utilizing static hosting for the frontend and an EC2-backed FastAPI backend, with DynamoDB as the primary data store.

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

## 2. AWS Cloud Infrastructure

### 2.1 DNS (Route 53)
**Hosted Zone ID:** `Z05016781BXLZFCXESLON` (`10xquery.com`)

| Record | Type | Target |
|--------|------|--------|
| `10xquery.com` | A, AAAA (Alias) | CloudFront Distribution (`d2d6yps1yamtai.cloudfront.net`) |
| `www.10xquery.com` | A, AAAA (Alias) | CloudFront Distribution (`d2d6yps1yamtai.cloudfront.net`) |
| `api.10xquery.com` | A, AAAA (Alias) | Application Load Balancer (`dualstack.ketzek-lb-130884797.us-east-1.elb.amazonaws.com`) |

### 2.2 Frontend Hosting (S3 + CloudFront)
- **S3 Bucket:** `s3://www.10xquery.com` configured for static website hosting (index and error document set to `index.html`).
- **CloudFront Distribution:** `E2BTNOZGJYMK2F` routes traffic to the S3 bucket website endpoint.
- **ACM Certificate:** `arn:aws:acm:us-east-1:912112639269:certificate/555c2636-a11f-4a10-8ef0-0770a857d211` covering `10xquery.com`, `www.10xquery.com`, and `api.10xquery.com`.

### 2.3 Backend Hosting (EC2 + ALB)
- **EC2 Instance:** `i-0f9e1d882c3ada3a4` running in `us-east-1` (same instance as Ketzek, but running a different service).
- **Target Group:** `10xquery-api-tg` routing traffic to port `8000` on the EC2 instance.
- **ALB Listener Rule:** The ALB listener on port 443 forwards requests with the Host header `api.10xquery.com` to the `10xquery-api-tg` target group.
- **Security Group:** `sg-0112515ae9e3bdba7` has an inbound rule allowing TCP traffic on port `8000` from `0.0.0.0/0`.

---

## 3. DynamoDB Schema

All tables use **PAY_PER_REQUEST** (on-demand) billing mode.

### 3.1 `10xquery_surveys` (Single-Table Design)
**Primary Key:** `PK` (String, HASH) + `SK` (String, RANGE)
**GSI:** `UserConversations` — `GSI1PK` (HASH) + `GSI1SK` (RANGE), projects ALL. Used to fetch surveys owned by a specific user.

| PK | SK | Description | Key Fields |
|----|-----|-------------|------------|
| `SRV#{surveyId}` | `META` | Survey metadata | `title`, `description`, `createdAt`, `ownerId`, `visibility` ("public"/"private"), `allowedUsers` (list of strings), `GSI1PK` (`USER#{userId}`), `GSI1SK` (timestamp) |
| `SRV#{surveyId}` | `DEFINITION` | Survey questions | `json` (dictionary of questions and types), `updatedAt` |
| `SRV#{surveyId}` | `RESP#{timestamp}#{respId}` | Participant response | `responderId`, `answers` (JSON of question IDs to answer values), `ip`, `ts` |

### 3.2 `10xquery_UserProfiles`
**Primary Key:** `userId` (String, HASH)
**GSIs:** `email-index`, `identityKey-index`

| Field | Type | Description |
|-------|------|-------------|
| `userId` | S | **Unique 8-digit numeric string** (e.g. "12345678"). Used for easy sharing for private surveys. |
| `email` | S | User email |
| `displayName` | S | User's display name |
| `passwordHash` | S | Argon2 password hash |
| `identityKey` | S | e.g. `google#{sub}` for OAuth integration |
| `createdAt` | S | ISO timestamp |

### 3.3 `10xquery_UserSessions`
**Primary Key:** `sessionId` (String, HASH)
**GSI:** `userId-index`

| Field | Type | Description |
|-------|------|-------------|
| `sessionId` | S | UUID session identifier |
| `userId` | S | Owner |
| `refreshHash` | S | SHA-256 of the refresh token cookie |
| `expiresAt` | S | ISO timestamp |
| `ttl` | N | DynamoDB TTL epoch for auto-expiration |

---

## 4. Frontend Architecture

### 4.1 Tech Stack
- **UI Framework:** Vanilla JavaScript with HTML5. No build steps required.
- **Styling:** Tailwind CSS loaded via CDN (`cdn.tailwindcss.com`) with a custom configuration injected in the `<head>` to support a dark-mode glassmorphism aesthetic.
- **API Communication:** Native `fetch()` calls to `api.10xquery.com` (or `localhost:8000` in dev).

### 4.2 Pages & Flow
| Page | Purpose |
|------|---------|
| `index.html` | **Creator Dashboard.** Handles Authentication (Login/Signup). Displays the unique 8-digit User ID. Contains the Surveys Dashboard, the Interactive Survey Editor (adding/removing complex questions), and the Results Dashboard (data table of all responses). |
| `survey.html` | **Participant View.** Public-facing page to take a survey (`?id=...`). Dynamically renders questions based on the JSON definition (text, longtext, radio, checkbox). Contains an authentication lock screen for `private` surveys requiring an allowed 8-digit User ID. |

### 4.3 Key JS Functions
- **`handleLogin(e)` / `handleCreateSurvey(e)` / `handleSubmit(e)`:** Ensure you always use `e.preventDefault();` synchronously in the inline `onsubmit` HTML attribute to prevent browser page reloads during async `fetch` calls.

---

## 5. Backend Architecture (10xquery-api)

### 5.1 Tech Stack
- **Framework:** FastAPI (Python 3.9+)
- **Server:** Uvicorn
- **Database Driver:** `boto3`
- **Auth:** PyJWT, argon2-cffi

### 5.2 Endpoints (`app/main.py` & `app/users.py`)
- **`/v1/users` & `/v1/auth/*`:** Registration, login, logout, and token refresh. Issues an `httpOnly` refresh token cookie and returns a short-lived access token. Generates an 8-digit unique `userId` on signup.
- **`/v1/surveys` (POST/GET):** Create new surveys or list surveys owned by the user.
- **`/v1/surveys/{id}` & `/v1/surveys/{id}/meta`:** Fetch and update survey metadata. Implements `_check_survey_access` to block unauthorized users from viewing private surveys.
- **`/v1/surveys/{id}/definition` (GET/PUT):** Read/Write the JSON structure of questions for a survey.
- **`/v1/surveys/{id}/responses` (POST/GET):** Submit a response (as a participant) or fetch all responses (as the owner) to populate the results data table.

---

## 6. Deployment Pipeline

Deployment is fully automated using a custom bash script. 

### 6.1 `push-and-publish`
Run `./push-and-publish "commit message"` from the root of the repository.
1. Commits and pushes the code to the GitHub repository (`Uzmany/10xquery-s3`).
2. Syncs the frontend files (`index.html`, `survey.html`, etc.) to the S3 bucket (`s3://www.10xquery.com/`).
3. Invalidates the CloudFront distribution cache (`E2BTNOZGJYMK2F`) to serve the latest UI.
4. Connects via SSH to the EC2 instance (`54.242.99.16`) using the key `ketzek_ai_backend_2.pem`.
5. Pulls the latest code, kills the old Uvicorn process for port 8000, and restarts the backend in the background using `nohup`.

### 6.2 Local Development
- Start the API: `cd 10xquery-api && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && uvicorn app.main:app --reload --port 8000`
- Serve the Frontend: Open `index.html` via Live Server or `python3 -m http.server 5173`. The JS code automatically detects `localhost` and points API calls to `http://localhost:8000`.

---

## 7. Known Behaviors & Next Steps for Agents

- **Form Submission Reloads:** Due to browser event loops, always enforce `event.preventDefault()` directly in the HTML element (e.g. `<form onsubmit="event.preventDefault(); submitFunc();">`) instead of just inside the async JS handler to prevent ghost reloads.
- **CORS Setup:** The backend explicitly allows origins `https://10xquery.com` and `https://www.10xquery.com`. If new subdomains are added, update `CORS_ORIGINS` in the environment or inside `app/main.py`.
- **Private Surveys:** The `_check_survey_access` method currently checks the `allowedUsers` array. When building a UI to share surveys, ensure users are inputting the exact 8-digit numeric strings.