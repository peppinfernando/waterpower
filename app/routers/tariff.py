from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import tariff as tariff_service
from app import schemas

router = APIRouter(prefix="/api/tariff", tags=["tariff"])


@router.get("/calculate", response_model=schemas.TariffResponse)
def calculate(
    start: date,
    end: date,
    target_margin_pct: float = 20.0,
    db: Session = Depends(get_db),
):
    result = tariff_service.calculate_tariff(db, start, end, target_margin_pct)
    return schemas.TariffResponse(**result)
