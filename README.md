# Invoice Flow: PDF to decision (Zamp ASA case study, PS-1)

Upload an invoice (clean PDF, scan or photo). The system reads it, extracts the key fields, checks them
against a procurement system (approved vendors, POs, tolerances, duplicates) and returns
**Approve / Hold / Reject** with the reasons, next steps and a drafted message. Each stage streams live.

## This folder

The deployable copy of Invoice Flow: the app, its settings, the sample data and nothing else (no tests, tools, local invoices or secrets). It is complete: `pip install -r requirements.txt` and `python run.py` run it, and `Dockerfile` / `render.yaml` deploy it. To use a Gemini or Claude key locally, copy `.env.example` to `.env` and fill it in; `.env` is git-ignored, so it isn't uploaded with git or GitHub Desktop.

## Run it

```bash
pip install -r requirements.txt           # includes RapidOCR (PP-OCRv6): scans, photos and handwriting are read locally, no API key or system install
python run.py                             # prints the link: http://localhost:8000
```

Optional environment variables. They can also go in a `.env` file in the project folder (see `.env.example`), which is loaded at startup and kept out of Docker images and git.

| Variable | Purpose | Default |
|---|---|---|
| `GEMINI_API_KEY` | Turns on the Google Gemini vision extractor (handwriting, odd layouts). Page images and OCR text are sent to Google. If Gemini is busy or unreachable, the built-in rules take over | unset: built-in rules + OCR |
| `GEMINI_MODEL` | Gemini model used for extraction | `gemini-3.5-flash-lite` |
| `ANTHROPIC_API_KEY` | Or use Claude instead (Gemini wins if both are set). Must be an Anthropic key (`sk-ant-...`); anything else is ignored with a warning | unset |
| `EXTRACTOR` | `auto` (LLM if key, else rules), `llm`, `rules` | `auto` |
| `EXTRACT_MODEL` | Model used for extraction | `claude-sonnet-5-5` |
| `STAGE_DELAY` | Seconds of pacing per stage so the live view is watchable (0 = full speed) | `0.35` |
| `AS_OF_DATE` | Date used for date sanity checks | today |
| `PORT`, `RUNTIME_DIR` | Port, where the SQLite DB, uploads and logs live | `8000`, `./runtime` |
| `LOG_LEVEL` | Console log level. `runtime/logs/invoice-flow.log` always keeps DEBUG (rotated at 10 MB, last 5 kept) | `INFO` |

## Accounts

Every login is separate: its own invoices, dashboard, PO ledger, approved vendors, duplicate history and Excel report. The vendor list and PO register in `data/` are shared sample data, and Reset clears only your own account. The first account created inherits any history from before logins existed.

## Going public

The app is one process with a SQLite file, so it needs a host that runs a Docker image and offers a **persistent disk** (without one, every redeploy deletes all accounts and invoices).

**On Render** (`render.yaml` is the blueprint): push this folder to a GitHub repository, then in Render choose New > Blueprint and pick the repository. It creates one Docker web service on the Standard plan (2 GB: reading a photo peaks around 570 MB, more than the 512 MB Starter plan has) with a 1 GB disk at `/data`, and asks for `GEMINI_API_KEY`, which is stored as a secret and never committed. `.env`, `runtime/` (your local invoices) and `.venv/` are git-ignored; This folder holds no secrets, so it can also go up through GitHub's web page (Add file > Upload files; it's under that page's 100-file limit). If you later add a `.env` or run the app here (which creates `runtime/`), push with git or GitHub Desktop instead: the web page ignores `.gitignore` and would publish them.

Anywhere else:
1. Deploy the `Dockerfile`, attach the disk at `/data`, and make sure the host serves it over HTTPS.
2. Run a single instance only: the invoice queue lives in memory.
3. A model key (`GEMINI_API_KEY` or `ANTHROPIC_API_KEY`) sends every visitor's documents to that provider at your cost. The sign-in page then says so, and `GEMINI_DAILY_LIMIT` caps the documents a day sent to Gemini (the blueprint uses 300); the rest are read with the built-in OCR and rules.
4. Create your own account first. To close sign-up afterwards, set `ALLOW_SIGNUP=0`.

Limits: the Docker image turns on `MAX_QUEUED_PER_USER` (25 waiting) and `MAX_RUNS_PER_USER` (1000 in all) per account, so no one account can fill the shared queue or the disk; a local copy runs without them (0 = no limit). `MAX_UPLOAD_MB` (25 MB per upload), `SESSION_DAYS` (14).

Not built yet: email verification and password reset. Documents are stored unencrypted on the server's disk, and are processed one at a time across all accounts, so a busy site queues.

## The pipeline (9 stages)

ingest, read (text layer, or OCR on an enhanced grayscale copy of the page; unsure words, usually handwriting, get a second, padded read), extract, validate and reconcile, verify vendor, match PO, duplicate check,
PO balance and tolerance, decision. Every finding has a severity (info, warn, hold, reject). **The decision is the
worst severity**, so there is no hidden score. The Rules tab in the app lists the rules.

Key behaviours
- **Grounding:** a total, invoice number, date or GSTIN is only trusted if it appears word-for-word in the document. Otherwise the invoice is held (`UNGROUNDED_FIELD`). This guards against LLM hallucination.
- **Tax:** billing is judged on the pre-tax value. If tax is embedded in a lump sum, GST is backed out using the printed rate (or the PO's rate).
- **PO matching:** explicit reference first. Otherwise an implied match scored on amount fit, description overlap and date. It needs at least 75% and a 15-point lead over the runner-up, or the invoice is held as ambiguous.
- **Split POs:** only approved invoices count against a PO. Billing is judged cumulatively: up to 2% over is approved, 2% to 10% goes to a person, over 10% is rejected.
- **Vendor:** matched by GSTIN first, then fuzzy name. Blocked vendors are rejected. A GSTIN/name mismatch is held.
- **Duplicates:** same file hash, or same vendor + number + amount, is rejected. Same number with a different amount, or same vendor + amount + date with a different or missing number, is held.
- **Human in the loop:** a held invoice can be approved or rejected with a note. Approving posts it to the PO ledger.
- **Vendor approval:** the Review page says whether the vendor is on the approved list, and offers to approve an unknown one before the checks run (`approve_vendor` on `/continue`; the older `/approve-vendor` endpoint still works for API use). The vendor joins the approved list (stored in SQLite, `data/vendors.csv` is untouched) and the invoice runs again. Blocked vendors, GSTIN mismatches and names resembling an existing vendor can't be approved this way. Approvals are listed on the PO ledger tab, can be removed there, and are cleared by Reset.
- **Step by step:** the Process tab walks a document through seven pages: New document, Read, Extract, Review, Checks, Decision, Stamp. An upload stops after extraction (status `review`) with the fields saved, so a person can check them and add details; `POST /api/runs/{id}/continue` then runs the remaining checks without reading the file again, even after a restart. Sample scenarios play through without stopping. At Decision a held invoice can be approved, kept on hold or rejected.
- **Deleting from history:** each History row's ✕ (two clicks) deletes the invoice for good with `DELETE /api/runs/{id}`: its record, file and PO ledger posting. Nothing is kept, so the same file uploaded again is checked as a new invoice. The trade-off: a deleted invoice can no longer be caught as a duplicate if a vendor resends it. To take an invoice out of a folder without deleting it, use Change folder at its Decision step.
- **Analytics tab:** the Dashboard has Overview and Analytics tabs, both following the folder and period picked above them (`GET /api/analytics`). Analytics counts rejected invoices too, and shows, in order: what needs you now (value and count by outcome, money on hold by age); spend over time (approved, by invoice date, daily / monthly / yearly); the system's decision against the final one (3×3); why invoices stop by week, under Invoice data / Vendor / PO & billing (a bar lists its invoices in History); fraud and control flags, split into "call the vendor" and "ask for a resend"; a vendor scorecard; and batch throughput. Every chart has a table view.
- **Folders:** the Decision step files an invoice into a folder, or a new one with any name. Each folder in the sidebar opens its own Dashboard (figures, charts, history and Excel report for that folder only), and its reset icon deletes the folder's invoices, files and PO postings after a second click; the folder stays, and approved vendors are kept. An approved invoice not yet filed goes automatically into the folder holding its vendor's most recently filed bill (also when a person approves a held one).
- **Vendor and PO answers at Decision:** a held invoice can be settled there. An unknown vendor can be approved (`POST /api/runs/{id}/approve-vendor`). For a PO hold, pick one of the vendor's open POs, or record that the vendor doesn't use POs (`POST /api/runs/{id}/po`). The invoice is then re-checked. "No PO" is remembered per vendor, so its later bills that cite no PO aren't held for one; a bill citing a PO that doesn't exist still needs the right PO. All other checks still run on every bill.
- **ZIP uploads:** a `.zip` can be chosen or dropped like any file. The server unpacks it and treats every PDF and photo inside (subfolders included, in path order) like a multi-file upload; system files, nested ZIPs and anything else are skipped. There is no limit on how many invoices an archive holds; the size guards are `MAX_UPLOAD_MB` per unpacked file and 2 GB in all, counted while unpacking so a crafted "zip bomb" can't get past them. Password-protected archives are refused, and names inside an archive never decide where a file is written.
- **Light / Dark theme:** the switch above the account at the bottom of the sidebar. The choice is remembered in the browser and applied before the page is drawn (`static/theme.js`), so a dark page doesn't flash light. All colours are tokens in `static/app.css`, with a dark set under `[data-theme="dark"]`.
- **Approvals step:** after files are chosen and before anything is uploaded, step 2 asks three things for the whole upload (`approvals` on `POST /api/runs`): auto-approve all the vendors (new identities are added as their invoices are checked; lookalikes of a vendor on file or a blocked one, and GSTIN disagreements, are still held), auto-approve all PO references whether present or not (a PO number missing from the register, or none, doesn't hold an invoice; another vendor's PO is still held and a registered PO is still checked against its balance; this applies to that upload only and isn't remembered), and a folder every invoice that isn't rejected goes into as it's decided. In a multi-file upload the invoices are processed strictly one at a time: the first is read and stops at Review, the others wait, and once its review is submitted it is checked to a decision before the next one is read.
- **Uploading a folder:** choosing several files in the New document upload box, or dropping several files or a whole folder on it, sends every PDF and photo as one batch (`POST /api/runs` with `batch=1`). Only the first invoice stops at Review. There you approve its vendor and answer the PO question for the whole folder: accept the PO numbers its invoices cite (when they aren't in the register) or mark the vendor as not using POs. The others wait, then run straight through the checks with those answers, and are approved if every check passes; anything failing a check is held as usual. The answers only apply to invoices from the same vendor, and are remembered for its later bills. "Approve all the new vendors" on that page adds every vendor in the batch that isn't on the list as its invoice is checked (`approve_all_vendors`), and the PO answer then covers those vendors too; a name resembling a vendor on file or a blocked one, or a GSTIN that disagrees, is still held. Once every invoice is checked, "Save all to a folder" under the batch's list files all of them that weren't rejected in one go (`POST /api/batches/{id}/folder`). Filing the first invoice in a folder also files the rest of the batch that passed. If the first file is unreadable, not an invoice, or cancelled, the next one asks instead. Waiting invoices count against `MAX_QUEUED_PER_USER` when that limit is set.
- **Learning from a manual approval:** approving a held bill also remembers the vendor and PO answers it implies, and the Decision page says so before and after. An unknown vendor joins the approved list. An ambiguous name with no GSTIN becomes another name for the approved vendor it resembles, keeping that vendor's POs; with its own GSTIN it's a new vendor. A cited PO number missing from the register is accepted for that vendor, and a bill citing none marks a new vendor (or one held over its PO) as not using POs. Fraud signals are never learned: a GSTIN that disagrees with the vendor record, a lookalike of a blocked vendor, or another vendor's PO. Removing an app-approved vendor on the Ledger page forgets what was learned for it.
- **Typed details:** the Review page supplies what a document doesn't show. Filling a gap raises a warning and the value is marked "typed in". A value that disagrees with the document holds the invoice for a person, so typing never silently overrides what was read.
- **Not an invoice:** mentioning invoices isn't enough, and a real bill may not say "invoice" legibly. A document counts as an invoice with a title at the top, a labelled number with a date or amounts, tax lines with a total, or a GSTIN and date with tax lines or line items. With two or more such details but not enough to be sure, it is held for a person (`INVOICE_UNCONFIRMED`). With fewer, including a file with no text at all, it is rejected, and a person can overrule that with "Yes, check it as an invoice", which re-checks it and always ends in a hold. With the Claude extractor on, the model classifies the document instead.
- **Rejected documents** stay off the Dashboard: its figures, charts and history cover approved and held invoices only. They remain on the Process tab and in the database.
- **Grouping and periods:** History groups by date (the default), month, year, vendor, invoice number, PO or outcome, with a count and total per group. The Monthly and Yearly filters use the invoice date and apply to the whole Dashboard and the Excel report; an invoice with no date appears only under All time. Choices are remembered per login in the browser.
- **Excel report:** the Dashboard downloads a workbook with live charts (outcomes, why invoices stop, approved value by vendor, PO billing) plus Invoices, PO ledger, Approved vendors and Blocked vendors sheets. Like the Dashboard it leaves rejected documents out of the figures, and lists them on their own Rejected sheet.
- **Approved vendor list:** the PO ledger tab also downloads the approved vendors as an Excel file of their own, with each vendor's approved invoice count and value.

## Demo order (synthetic invoices, use **Reset** first)

Use the scenario list in the left column (or "Run all in order").

| # | Scenario | Shows | Outcome |
|---|---|---|---|
| 01 | Clean PDF, explicit PO | Happy path, full trail | Approve |
| 02 | Lump sum, GST inside total | Tax backed out | Approve |
| 03 | No PO reference | PO inferred from amount and description | Approve (noted) |
| 04, 05 | One PO billed in two invoices | Split PO, cumulative ledger | Approve, Approve |
| 06 | A third invoice on the same PO | Cumulative 108% | Hold |
| 07 vs 08 | +1.2% vs +6% | Near-miss tolerance | Approve vs Hold |
| 09 | Same invoice sent again | Exact duplicate | Reject |
| 10 | No invoice number, same amount as an earlier one | Missing field + possible duplicate | Hold |
| 11 | Vendor not on the approved list | Vendor check | Hold |
| 12, 13 | Scanned image: clean / missing date | OCR path, missing critical field | Approve / Hold |
| 14 | Lines don't add to subtotal | Arithmetic check | Hold |

Self-designed edge cases worth calling out in the video: **(a)** split PO that goes over on the third invoice,
**(b)** possible duplicate with the invoice number missing, **(c)** scan where a critical field is absent, **(d)** near-miss amounts
either side of the tolerance line.

## Assumptions and limits

- Vendors, POs and invoices are synthetic (`data/`, `samples/`). The buyer is a made-up company.
- Tolerance bands (2% / 10%) and the 75% / 15-point implied-PO thresholds are my own choices, set in `app/config.py`.
- Rules-only extraction is tuned for printed invoices. Handwriting and photos need the LLM extractor.
- The Claude extractor is implemented but was not exercised in my build environment (no API key). It falls back to rules on any failure, and the grounding check applies to its output too.
- One worker processes runs in order on purpose, so two split invoices can't both pass on the same "billed so far".
- Single currency (INR) is assumed. Other currencies are flagged.
