import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import customer_report as customer_report_service
from app import schemas

router = APIRouter(prefix="/api/customer-report", tags=["customer-report"])


@router.post("/upload", response_model=schemas.CustomerReportResponse)
async def upload_customer_report(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "Please upload an .xlsx Excel file (if yours is .xls, open it in Excel and use Save As > Excel Workbook).")

    content = await file.read()
    if len(content) > customer_report_service.MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File is too large (max 15MB).")
    if not content:
        raise HTTPException(400, "The uploaded file is empty.")

    records, metadata, parse_warnings, errors = customer_report_service.parse_mprn_usage_xlsx(content)
    if errors:
        raise HTTPException(400, " ".join(errors))

    days, report_warnings = customer_report_service.build_customer_report(db, records)

    return schemas.CustomerReportResponse(
        mprn=metadata.get("mprn"),
        profile_description=metadata.get("profile_description"),
        prices_are_simulated=(os.getenv("SEMO_CLIENT", "mock").lower() == "mock"),
        days=days,
        warnings=parse_warnings + report_warnings,
    )
