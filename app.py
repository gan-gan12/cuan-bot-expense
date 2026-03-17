from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from telegram import Update

from expense_bot.charts import ExpenseChartService
from expense_bot.config import Settings, get_settings
from expense_bot.db import ExpenseDB
from expense_bot.ocr import ReceiptOCR
from expense_bot.service import ExpenseService
from expense_bot.telegram_app import create_telegram_application


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _build_runtime(settings: Settings) -> dict[str, object]:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum diisi.")
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL belum diisi.")

    db = ExpenseDB(database_url=settings.database_url, timezone_name=settings.bot_timezone)
    db.open()
    db.ensure_schema()

    service = ExpenseService(db=db, timezone_name=settings.bot_timezone)
    receipt_ocr = ReceiptOCR(
        api_token=settings.huggingface_api_token,
        ocr_backend=settings.ocr_backend,
        ocr_backend_url=settings.ocr_backend_url,
        custom_payload_mode=settings.custom_payload_mode,
        endpoint_url=settings.florence_endpoint_url,
        model_id=settings.florence_model_id,
        gemini_api_key=settings.gemini_api_key,
    )
    chart_service = ExpenseChartService(
        db=db,
        quickchart_url=settings.quickchart_url,
        timezone_name=settings.bot_timezone,
    )
    telegram_application = create_telegram_application(
        token=settings.telegram_bot_token,
        service=service,
        receipt_ocr=receipt_ocr,
        chart_service=chart_service,
    )
    return {
        "db": db,
        "service": service,
        "receipt_ocr": receipt_ocr,
        "chart_service": chart_service,
        "telegram_application": telegram_application,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    runtime = _build_runtime(settings)
    telegram_application = runtime["telegram_application"]

    await telegram_application.initialize()
    await telegram_application.start()

    app.state.settings = settings
    app.state.db = runtime["db"]
    app.state.service = runtime["service"]
    app.state.receipt_ocr = runtime["receipt_ocr"]
    app.state.chart_service = runtime["chart_service"]
    app.state.telegram_application = telegram_application

    try:
        yield
    finally:
        await telegram_application.stop()
        await telegram_application.shutdown()
        app.state.db.close()


app = FastAPI(title="CuanBot Webhook", version="2.0.0", lifespan=lifespan)


@app.get("/")
async def root(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "ok": True,
        "service": "cuanbot-webhook",
        "webhook_url": settings.webhook_url,
        "ocr_backend": settings.ocr_backend,
        "ocr_enabled": True,
    }


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "ok": True,
        "database": bool(settings.database_url),
        "telegram": bool(settings.telegram_bot_token),
        "webhook_secret": bool(settings.telegram_webhook_secret),
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    settings = request.app.state.settings
    if settings.telegram_webhook_secret:
        secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(secret_header, settings.telegram_webhook_secret):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Webhook secret tidak valid.",
            )

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payload update tidak valid.")

    telegram_application = request.app.state.telegram_application
    update = Update.de_json(payload, telegram_application.bot)
    await telegram_application.process_update(update)
    return {"ok": True}


@app.get("/telegram/webhook-info")
async def webhook_info(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    _validate_setup_secret(request, settings)
    info = await request.app.state.telegram_application.bot.get_webhook_info()
    return {
        "url": info.url,
        "pending_update_count": info.pending_update_count,
        "last_error_date": str(info.last_error_date) if info.last_error_date else None,
        "last_error_message": info.last_error_message,
        "max_connections": info.max_connections,
    }


@app.post("/telegram/setup-webhook")
async def setup_webhook(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    _validate_setup_secret(request, settings)

    if not settings.webhook_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PUBLIC_BASE_URL atau VERCEL_URL belum tersedia.",
        )

    telegram_application = request.app.state.telegram_application
    await telegram_application.bot.set_webhook(
        url=settings.webhook_url,
        secret_token=settings.telegram_webhook_secret or None,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    info = await telegram_application.bot.get_webhook_info()
    return {
        "ok": True,
        "url": info.url,
        "pending_update_count": info.pending_update_count,
    }


@app.delete("/telegram/webhook")
async def delete_webhook(request: Request) -> dict[str, bool]:
    settings = request.app.state.settings
    _validate_setup_secret(request, settings)
    telegram_application = request.app.state.telegram_application
    await telegram_application.bot.delete_webhook(drop_pending_updates=False)
    return {"ok": True}


def _validate_setup_secret(request: Request, settings: Settings) -> None:
    if not settings.webhook_setup_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WEBHOOK_SETUP_SECRET belum diisi.",
        )
    secret_header = request.headers.get("X-Setup-Secret", "")
    if not secrets.compare_digest(secret_header, settings.webhook_setup_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Setup secret tidak valid.",
        )
