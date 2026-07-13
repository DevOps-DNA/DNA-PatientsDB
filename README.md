# POD Matcher

Internal DNAssociates tool: upload scanned Proof-of-Delivery (POD) invoices,
Purchase Order copies, and the Outward Details workbook. It OCRs the PODs,
parses the Purchase Orders, matches each patient's POD to their Purchase
Order (verified against the Outward Details workbook), and produces a merged
PDF (POD page, then Purchase Order page, per patient) plus a CSV audit report.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install tesseract   # or apt-get install tesseract-ocr on Linux

uvicorn app:app --app-dir backend --reload --port 8420
```

Open http://localhost:8420.

## Deployment (Railway)

This repo ships a `Dockerfile` (installs `tesseract-ocr` plus Python deps)
and a `railway.json` pointing Railway at it. In the Railway dashboard:
create a new project, deploy from this GitHub repo, and Railway will build
and run the Dockerfile automatically. It reads the `PORT` env var Railway
injects at runtime — no extra config needed.

## Notes

- Job uploads/output are written to `jobs/` at runtime and are not
  persisted across deploys (ephemeral filesystem) — that's fine, each job
  is a one-off batch run.
- No patient data is committed to this repo (see `.gitignore`). Sample
  PDFs/xlsx used for local testing should stay out of version control.
