# EPD upload-extract-review-store prototype

Run:

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

This Phase 1 build supports native-text PDF EPDs.
It does not use OCR or an external LLM.
All extracted data must be reviewed before storage.
