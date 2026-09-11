from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import usage_ingest
from app import schemas

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.post("/upload", response_model=schemas.UsageUploadResponse)
async def upload_usage(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "Please upload a .csv file.")

    content = await file.read()
    if len(content) > usage_ingest.MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File is too large (max 10MB).")
    if not content:
        raise HTTPException(400, "The uploaded file is empty.")

    records, warnings, errors = usage_ingest.parse_usage_csv(content)
    if errors:
        raise HTTPException(400, " ".join(errors))

    result = usage_ingest.upsert_usage_intervals(db, records)

    # Force the next view of this range to rebuild from the real usage
    # data rather than silently keep serving cached notional-based costs
    # (see the docstring on invalidate_cost_records for why this step
    # is necessary, not just tidy).
    start = datetime.strptime(result["date_range_start"], "%Y-%m-%d")
    end = datetime.strptime(result["date_range_end"], "%Y-%m-%d") + timedelta(days=1)
    usage_ingest.invalidate_cost_records(db, start, end)

    return schemas.UsageUploadResponse(
        rows_parsed=len(records),
        rows_inserted=result["inserted"],
        rows_updated=result["updated"],
        date_range_start=result["date_range_start"],
        date_range_end=result["date_range_end"],
        warnings=warnings,
    )
