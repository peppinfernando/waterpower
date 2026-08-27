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
    network_charge_eur_per_kwh: float = 0.0,
    pso_levy_eur_per_month: float = 0.51,
    vat_pct: float = 9.0,
    db: Session = Depends(get_db),
):
    result = tariff_service.calculate_tariff(
        db, start, end, target_margin_pct,
        network_charge_eur_per_kwh, pso_levy_eur_per_month, vat_pct,
    )
    return schemas.TariffResponse(**result)
