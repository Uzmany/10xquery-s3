# Project status — 10xQuery

> **Current status:** STOPPED
> **Stopped on:** May 16, 2026
> **Reason:** Cost optimization. The whole AWS account was running ~$190/mo while only BookTwoLang was actively used. 10xQuery's backend and CloudFront are paused; data and scaffolding are preserved.

## What was done on May 16, 2026

| Resource | Action | Reversible? |
|---|---|---|
| EC2 uvicorn on `port 8000` (`i-0f9e1d882c3ada3a4`) | Confirmed stopped (was already not running) | Yes |
| CloudFront `E2BTNOZGJYMK2F` (apex + www) | **Disabled** | Yes — flip `Enabled: true` |
| ALB listener rule (priority 10, Host = `api.10xquery.com`) | **KEPT** on shared `ketzek-lb` — costs nothing, makes restart one less step | n/a |
| Target group `10xquery-api-tg` | **KEPT** — points at the shared EC2 on port 8000 | n/a |
| Route 53 zone `10xquery.com` | **KEPT** (`Z05016781BXLZFCXESLON`) — $0.50/mo, makes restart trivial | n/a |
| Domain `10xquery.com` | **KEPT**, auto-renew on | n/a |
| S3 bucket `www.10xquery.com` | **KEPT** | n/a |
| DynamoDB `10xquery_surveys`, `10xquery_UserProfiles`, `10xquery_UserSessions` | **KEPT** (`PAY_PER_REQUEST`) | n/a |
| GitHub repo `Uzmany/10xquery-s3` | **KEPT** | n/a |

## How to bring 10xQuery back online

```bash
# 1. SSH to the shared EC2 box
ssh -i ~/.ssh/ketzek_ai_backend_2.pem ec2-user@54.242.99.16

# 2. Pull latest + start uvicorn on :8000
cd /home/ec2-user/10xquery-s3
git pull
cd 10xquery-api
source .venv/bin/activate
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  > /tmp/10xquery.log 2>&1 &
curl -s http://localhost:8000/   # sanity check
exit

# 3. Re-enable the CloudFront distribution
ID=E2BTNOZGJYMK2F
aws cloudfront get-distribution-config --id "$ID" > /tmp/cf.json
ETAG=$(jq -r .ETag /tmp/cf.json)
jq '.DistributionConfig.Enabled = true | .DistributionConfig' /tmp/cf.json > /tmp/cf-new.json
aws cloudfront update-distribution --id "$ID" \
  --distribution-config file:///tmp/cf-new.json --if-match "$ETAG"
```

`10xquery.com` should be back online ~15 min after step 3.

## What's still costing money for 10xQuery while stopped

- Route 53 hosted zone: **$0.50/mo**
- DynamoDB / S3 storage: **~$0**
- Domain renewal: **~$15/yr** (Feb 20, 2027)
- EC2 / ALB: **$0 attributable** (shared with BookTwoLang)

**Total ongoing cost while stopped: ~$0.50/mo.**

## Notes

- The `push-and-publish` script still works to push code + sync frontend to S3; only the backend restart step is needed.
- If you want to also turn off the ALB listener rule for `api.10xquery.com` (priority 10 on `ketzek-lb`), it's harmless to delete — but adding it back later is two CLI lines. Cheaper to leave it.
