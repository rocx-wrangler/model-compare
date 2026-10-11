# Model Compare (PoC)

One prompt -> up to 4 DigitalOcean serverless-inference models in parallel -> side-by-side answers,
timing/token table, and lightweight "prefer this answer" / 1-5 rating (kept in the browser only).

## Run locally
    python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    export MODEL_ACCESS_KEY="your-model-access-key"          # Windows: set MODEL_ACCESS_KEY=...
    uvicorn main:app --reload --port 8000
Open http://localhost:8000

## Environment variables
| Name | Default | Purpose |
|---|---|---|
| MODEL_ACCESS_KEY | required | Server-side inference key (never sent to the browser) |
| ACCESS_CODE | none | If set, the page asks for this code (use on a public deployment) |
| CLIENT_IP_HEADER | none | Header holding the real client IP. On App Platform: `do-connecting-ip` |
| RATE_LIMIT_PER_MIN | 10 | Comparisons per client per minute |
| GLOBAL_RATE_LIMIT_PER_MIN | 60 | Comparisons per minute across all clients (spend ceiling) |
| MAX_MODELS | 4 | Models per comparison, enforced on the server |
| TOKEN_CAP | 4000 | Hard cap on output tokens per model (also limited by each model's max_output_tokens) |
| MODEL_ALLOWLIST | none | Comma-separated model IDs; if set, only these are allowed |

## Deploy on DigitalOcean App Platform
1. Push this folder to a GitHub repo (main.py, requirements.txt and static/ at the repo root).
2. Console > Create > App Platform > GitHub > pick repo and branch (autodeploy on).
3. Resource type: Web Service, 1 instance, smallest size. Run command:
   `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Environment variables: MODEL_ACCESS_KEY (encrypt), ACCESS_CODE (encrypt), CLIENT_IP_HEADER=do-connecting-ip.
5. Health check: HTTP, path `/api/config`.
6. Deploy, open the app URL, test, and check Runtime Logs.

