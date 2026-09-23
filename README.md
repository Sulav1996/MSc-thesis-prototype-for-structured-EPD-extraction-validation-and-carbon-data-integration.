# EPD Carbon Prototype — upload, extract, categorise, review, store

Streamlit prototype for the thesis workflow **EPD (PDF or ILCD+EPD XML) → Step 1 DGNB parameters →
Step 2 category → human review → versioned storage (local or Google Drive) → search / compare / export**.
No external AI API is used; every extracted value keeps its page, method and confidence.

## Run locally

```bash
python -m pip install -r requirements.txt
python -m streamlit run frontpage.py
python -m unittest discover -s tests -v      # regression tests
```

On first start the records in `data/epds.json` (first prototype, schema 1.1) are migrated into the
new store automatically. The JSON file itself is not changed.

## What each step does

| Step (Claude instruction 1) | Where | What happens |
| --- | --- | --- |
| **Step 1** – standard EPD information for DGNB | `extraction/metadata.py`, `extraction/tables.py`, `extraction/step1_dgnb.py` | Extracts only the Step 1 parameters + identity fields: EN 15804 version, ISO 14025 third-party verification, programme operator (checked against the ECO Platform list), RSL, conversion factor (ILCD `M / f = m`), module declarations and DGNB module scope, GWP/ODP/AP/EP/POCP/ADP, primary energy, fresh water, waste, electricity mix (residual mix?), product-specific data, Danish end-of-life context, kg CO₂e/m²/year helper (50 years, D separate). Each parameter gets pass / warning / fail / missing with evidence. |
| **Step 2** – categorise | `extraction/categorizer.py` | Rule-based, weighted keyword evidence (EN/DA/DE) from product name, ILCD classification, PCR, description and text → steel, concrete, window, skylight/roof window, door, insulation, roofing, wall/façade, timber, masonry, glass, boards, … with confidence, alternatives, ÖKOBAUDAT category and CPA hint. The reviewer can override it. |
| **Step 3** – ILCD dictionary | `tools/build_ilcd_dictionary.py`, `tools/dictionary_specs.py` → `data/epd_dictionary.json`, `data/ilcd_reference.json` | All 86 Step 3 columns (10 sections) mapped to ILCD+EPD v1.3 XPaths with requirement level, datatype, ILCD/InData definitions, EN 15804+A2 clause, ECO Platform conformity, ISO 22057 GUID, indicator UUIDs, units and PDF label aliases. Built from the `ILCD pdf` folder; the v3 keys are kept unchanged. |

Machine-readable EPDs: upload an ILCD+EPD process XML or an ILCD ZIP (e.g. ÖKOBAUDAT / ECO Portal export).
Values are matched by indicator UUID (confidence 1.0); declared unit and mass come from the reference flow in the ZIP.

Set `EPD_EXTRACTION_PROFILE=extended` to keep every indicator and the optional Step 3 fields
(composition, software/database) found in a PDF. The default profile keeps only the Step 1 indicators.

## Rebuild the dictionary

The `ILCD pdf` folder (667 MB) stays on your laptop and is git-ignored. After changing
`tools/dictionary_specs.py` or updating the ILCD reference files, run:

```bash
python tools/build_ilcd_dictionary.py          # writes data/epd_dictionary.json + data/ilcd_reference.json
python tools/build_ilcd_dictionary.py --check  # validate only
```

The build fails if a Step 3 field points to an XPath that does not exist in the ILCD+EPD v1.3 field table.

## Storage and transfer model

```
epd_store/
  transfer.py   versioned JSON record (schema 2.0), epd_uid, long-format results, CSV/JSONL, bundles
  index_db.py   SQLite index: epd, epd_result (long format), epd_module, epd_step1, epd_version, blob
  blobstore.py  LocalBlobStore | GoogleDriveBlobStore (+ local cache)
  gdrive.py     Google Drive v3 REST client (resumable uploads for large files)
  store.py      EPDStore: save_record, get_record, search, results, versions, bundles, rebuild_index
```

* Every approved save writes the source file (content-addressed by SHA-256, so it is never stored twice),
  the extracted text, a new **record version** (`records/<epd_uid>/v0001.json.gz`) and updates the index.
  Identical re-saves are detected and skipped.
* The index is derived data. **Storage & Transfer → Rebuild index** recreates it from the record documents.
* **Transfer bundles** (`.zip`: manifest, records, source files, long CSV) move the whole database
  between your laptop and the Render server (export on one, import on the other).
* Archive = soft delete (hidden from searches; all versions are kept).

### Google Drive storage (needed on Render)

Render's free web service has an **ephemeral disk**: anything the app writes is lost on every deploy,
restart or spin-down after 15 minutes idle, and persistent disks are only available on paid plans.
The app can store everything in your own Google Drive (your Google One quota) instead:

1. Google Cloud console → new project → enable **Google Drive API**.
2. OAuth consent screen: External, add yourself as test user, then **Publish app** (in *Testing*
   status Google expires refresh tokens after 7 days). The app only asks for the non-sensitive
   `drive.file` scope, so it can only see files it created.
3. Credentials → OAuth client ID → **Desktop app** → download the JSON.
4. On your laptop: `python tools/google_drive_auth.py --client-secrets client_secret.json`
   (add `--write-secrets` to also create `.streamlit/secrets.toml` for local runs).
5. Render → service → Environment: set `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`,
   `GDRIVE_REFRESH_TOKEN` (the blueprint in `render.yaml` already declares them). With
   `EPD_STORAGE_BACKEND=auto` the app switches to Drive when these are present.

A service account is not used on purpose: service accounts have no storage quota of their own, so
uploads into a personal Drive fail.

Files appear in Drive under `EPD_Prototype_Store/{files,text,records,db}`. On start-up the server
downloads the index snapshot (`db/epd_index.sqlite`) and re-indexes any record saved after it.

## Settings (environment variables or `.streamlit/secrets.toml`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `EPD_STORAGE_BACKEND` | `auto` | `local`, `gdrive`, or `auto` (Drive when credentials exist) |
| `EPD_DATA_DIR` | `data/store` | index, local blobs and Drive cache |
| `GDRIVE_FOLDER_NAME` | `EPD_Prototype_Store` | Drive folder created by the app |
| `EPD_SNAPSHOT_MAX_MB` | `50` | auto-upload the index snapshot after each save below this size |
| `EPD_EXTRACTION_PROFILE` | `step1` | `extended` keeps all indicators and optional fields |

## Repository hygiene

`.venv/` and `__pycache__/` were committed earlier. Remove them from git once (files stay on disk):

```bash
git rm -r --cached .venv __pycache__ extraction/__pycache__ validation/__pycache__
git commit -m "Stop tracking virtual environment and bytecode"
```

## Limitations

* No OCR: scanned pages are listed and need manual entry.
* PDF layouts vary. The table parser uses detected tables first and positioned words second, but
  every record still needs human review before it is saved.
* ECO Platform membership, DGNB/BR rules and the electricity-mix wording are dictionary data
  (`step1_dgnb_profile`, `eco_platform_programme_operators`). Update them in
  `tools/dictionary_specs.py` and rebuild when the rules change.
