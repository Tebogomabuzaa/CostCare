# CostCare on AWS

CostCare runs **serverless on AWS** in the **Africa (Cape Town) `af-south-1`** region, close to its South African
users. The entire stack is defined as infrastructure-as-code in [`deploy/template.yaml`](../deploy/template.yaml)
(AWS CloudFormation with the AWS SAM transform) and deployed with one command.

## Architecture

```mermaid
flowchart LR
    user(["Travellers & admins<br/>(browser)"]) -->|HTTPS| apigw["Amazon API Gateway<br/>HTTP API"]
    apigw --> web["AWS Lambda<br/>costcare-web<br/>FastAPI + Mangum · Python 3.12"]
    eb["Amazon EventBridge<br/>daily 03:00 SAST"] --> sync["AWS Lambda<br/>costcare-google-sync"]

    web --> sm[("AWS Secrets Manager<br/>app + admin secrets")]
    sync --> sm
    web --> s3[("Amazon S3<br/>price spreadsheet archive")]
    web --> db[("Supabase<br/>PostgreSQL")]
    sync --> db
    web --> openai["OpenAI API<br/>query understanding"]
    web --> places["Google Places API<br/>reviews & locations"]
    sync --> places

    web -. logs · metrics · traces .-> cw["Amazon CloudWatch<br/>dashboard · alarms · logs<br/>+ AWS X-Ray"]
    sync -.-> cw
    apigw -. access logs .-> cw
    cw -->|alarm| sns["Amazon SNS<br/>email alerts"]
```

| AWS service | What CostCare uses it for |
|---|---|
| **Amazon API Gateway (HTTP API)** | Public HTTPS endpoint for the website and REST API, with throttling and JSON access logs. |
| **AWS Lambda** | `costcare-web` runs the FastAPI app (AI search, provider pages, admin dashboard, Excel import) through the Mangum adapter. `costcare-google-sync` refreshes Google Business Profile data. |
| **AWS Secrets Manager** | Stores `SECRET_KEY`, the database URL, OpenAI/Google API keys, and a generated admin password. Loaded once per cold start; nothing sensitive is kept in code or environment variables. |
| **Amazon S3** | Encrypted, versioned, private bucket that archives every uploaded price spreadsheet for auditing. Moves files to Infrequent Access after 30 days. |
| **Amazon EventBridge** | Daily schedule that triggers the Google reviews/ratings/location refresh (Google's terms require regular refreshes of cached Places data). |
| **Amazon CloudWatch** | Log groups with 30-day retention, a `CostCare-prod` dashboard, and alarms for Lambda errors, API 5xx responses, p95 latency and failed syncs. |
| **AWS X-Ray** | Request tracing across API Gateway and Lambda, including calls to OpenAI, Google and the database. |
| **Amazon SNS** | Sends alarm notifications by email. |
| **AWS IAM** | Least-privilege roles generated per function (read two secrets, write to one bucket, write logs/traces). |

The database is **Supabase Postgres**, reached over TLS from Lambda. Lambda doesn't run inside a VPC, so it
doesn't need a NAT gateway, which keeps costs close to zero.

## Deploy

Prerequisites: AWS CLI v2 configured (`aws configure`) and Python 3 on your PATH. Docker isn't needed.

```bash
.\deploy\deploy.ps1
```

The script:
1. Builds a Linux-compatible Lambda package (`pip install --platform manylinux…`) into `build/lambda`.
2. Creates a private artifacts bucket and runs `aws cloudformation package`.
3. Runs `aws cloudformation deploy` for the `costcare-prod` stack and prints the website URL.

Options: `-Region af-south-1 -Stage prod -AlarmEmail you@example.com -SkipBuild`.

### After the first deploy

1. **Admin login:** AWS console → Secrets Manager → `costcare/prod/admin` → *Retrieve secret value*.
2. **Connect the real database and AI keys:** Secrets Manager → `costcare/prod/app` → *Retrieve secret value* →
   *Edit*. Fill in:
   - `DATABASE_URL`: Supabase pooler URI, e.g. `postgresql+psycopg://postgres.<ref>:<password>@<host>:6543/postgres`
   - `OPENAI_API_KEY`, `GOOGLE_MAPS_API_KEY`, optionally `ADMIN_API_KEY`
   - Leave `SECRET_KEY` unchanged.
3. New Lambda instances pick up the values automatically. To apply them immediately, redeploy
   (`.\deploy\deploy.ps1 -SkipBuild`) or update any function setting.

**Demo mode:** until `DATABASE_URL` is set, each Lambda instance uses a temporary SQLite database in `/tmp`
seeded with the sample providers. That's fine for a demo, but admin changes don't persist and can differ
between instances. Set `DATABASE_URL` for real use.

## Costs

For a low-traffic launch this is roughly **US$1–2 per month**:
- Lambda, API Gateway, X-Ray and CloudWatch alarms: mostly within the AWS Free Tier.
- Secrets Manager: US$0.40 per secret per month (2 secrets).
- S3 and CloudWatch Logs: cents.

Supabase, OpenAI and Google Places are billed separately by those providers.

## Limits to know

- API Gateway + Lambda accept request bodies up to about 6 MB, so keep uploaded spreadsheets below ~4 MB.
- API Gateway times out after 30 seconds. Long Google syncs run in the separate scheduled function, which
  has a 5-minute timeout.

## Tear down

```bash
aws cloudformation delete-stack --stack-name costcare-prod --region af-south-1
```

The uploads bucket is retained on purpose so archived spreadsheets aren't lost. Empty and delete it manually
if you no longer need it.
