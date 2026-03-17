from __future__ import annotations

import logging
from datetime import datetime
from io import BytesIO

from telegram import InputFile, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import get_settings
from .charts import ExpenseChartService
from .db import PendingReceipt
from .ocr import ReceiptOCR
from .parser import format_date_id, format_idr, parse_amount_from_text, parse_date_input
from .service import ExpenseService


logger = logging.getLogger(__name__)


def create_telegram_application(
    token: str,
    service: ExpenseService,
    receipt_ocr: ReceiptOCR,
    chart_service: ExpenseChartService,
) -> Application:
    application = Application.builder().token(token).updater(None).build()

    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message:
            await update.message.reply_text(service.help_text())

    async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message:
            await update.message.reply_text(service.help_text())

    async def total_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        joined_args = " ".join(context.args).lower()
        if "hari" in joined_args:
            await _reply_chunks(update.message, service.render_period_report(_user_key(update), "today"))
            return
        if "minggu" in joined_args:
            await _reply_chunks(update.message, service.render_period_report(_user_key(update), "week"))
            return
        if "bulan" in joined_args:
            await _reply_chunks(update.message, service.render_period_report(_user_key(update), "month"))
            return
        await update.message.reply_text(service.render_summary(_user_key(update)))

    async def total_hari_ini_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await _reply_chunks(update.message, service.render_period_report(_user_key(update), "today"))

    async def total_minggu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await _reply_chunks(update.message, service.render_period_report(_user_key(update), "week"))

    async def total_bulan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await _reply_chunks(update.message, service.render_period_report(_user_key(update), "month"))

    async def list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        limit = 10
        if context.args and context.args[0].isdigit():
            limit = max(1, min(50, int(context.args[0])))
        await _reply_chunks(update.message, service.render_recent_list(_user_key(update), limit=limit))

    async def hapus_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await update.message.reply_text(service.reply_delete(_user_key(update), context.args))

    async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await update.message.reply_text(service.reply_reset(_user_key(update), context.args))

    async def budget_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        raw_text = _build_command_text("budget", context.args)
        await update.message.reply_text(service.reply_budget(_user_key(update), raw_text, context.args))

    async def grafik_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        try:
            chart_bytes = await chart_service.render_monthly_category_chart(_user_key(update))
        except Exception:
            logger.exception("Gagal membuat grafik pengeluaran")
            await update.message.reply_text("Grafiknya belum berhasil dibuat. Coba lagi sebentar ya.")
            return

        await update.message.reply_document(
            document=InputFile(BytesIO(chart_bytes), filename=chart_service.build_filename()),
            caption="Grafik pengeluaran bulan ini.",
        )

    async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.photo:
            return

        if not receipt_ocr.enabled:
            await update.message.reply_text(
                "Fitur scan struk belum aktif. Hubungi admin untuk mengaktifkan OCR ya."
            )
            return


        largest_photo = update.message.photo[-1]
        tg_file = await largest_photo.get_file()
        buffer = BytesIO()
        await tg_file.download_to_memory(out=buffer)

        try:
            result = await receipt_ocr.scan_receipt(buffer.getvalue())
        except Exception:
            logger.exception("Gagal memproses gambar struk")
            await update.message.reply_text(
                "Maaf, scan struknya belum berhasil sekarang. Coba foto lebih terang atau kirim ulang ya."
            )
            return

        if not result:
            await update.message.reply_text(
                "Aku belum bisa baca struknya sekarang. Coba kirim foto yang lebih jelas ya."
            )
            return

        if result.needs_manual_total_confirmation or not result.receipt:
            await update.message.reply_text(result.reply_text)
            return

        receipt = result.receipt
        service.save_pending_receipt(
            PendingReceipt(
                user_key=_user_key(update),
                item=receipt.item,
                amount=receipt.total,
                category=receipt.kategori,
                expense_date=receipt.expense_date or datetime.now(service.tz).date(),
                raw_payload=result.structured_data or receipt.to_json(),
                is_bank_transaction=receipt.is_bank_transaction,
            )
        )
        await update.message.reply_text(result.reply_text)

    async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return

        user_key = _user_key(update)
        text = update.message.text.strip()
        if len(text) > 100:
            text = text[:100]
            
        pending = service.get_pending_receipt(user_key)
        low = text.lower()

        if pending:
            if low in {"simpan", "ya", "y", "oke", "ok"}:
                response = service.record_expense(
                    user_key=user_key,
                    item=pending.item,
                    amount=pending.amount,
                    category=pending.category,
                    expense_date=pending.expense_date,
                )
                service.clear_pending_receipt(user_key)
                await update.message.reply_text(response)
                return
            if low.startswith("ubah total"):
                new_amount = parse_amount_from_text(text)
                if not new_amount or new_amount <= 0:
                    await update.message.reply_text("Format ubah total: `ubah total 125000`")
                    return
                pending.amount = new_amount
                pending.raw_payload["total"] = new_amount
                service.update_pending_receipt(pending)
                await update.message.reply_text(
                    f"Siap, total diubah jadi {format_idr(new_amount)}. Balas `simpan` atau lanjut ubah."
                )
                return
            if low.startswith("ubah kategori"):
                new_category = text[len("ubah kategori") :].strip()
                if not new_category:
                    await update.message.reply_text("Format ubah kategori: `ubah kategori Makanan & Minuman`")
                    return
                pending.category = new_category
                pending.raw_payload["kategori"] = new_category
                service.update_pending_receipt(pending)
                await update.message.reply_text(
                    f"Siap, kategori diubah jadi {new_category}. Balas `simpan` atau lanjut ubah."
                )
                return
            if low.startswith("ubah merchant"):
                new_merchant = text[len("ubah merchant") :].strip()
                if not new_merchant:
                    await update.message.reply_text("Format ubah merchant: `ubah merchant Nama Toko`")
                    return
                pending.item = (
                    f"Transfer ke {new_merchant}" if pending.is_bank_transaction else f"Belanja {new_merchant}"
                )
                pending.raw_payload["item"] = pending.item
                service.update_pending_receipt(pending)
                await update.message.reply_text(
                    f"Siap, item diubah jadi {pending.item}. Balas `simpan` atau lanjut ubah."
                )
                return
            if low.startswith("ubah tanggal"):
                new_date_text = text[len("ubah tanggal") :].strip()
                new_date = parse_date_input(new_date_text)
                if not new_date:
                    await update.message.reply_text("Format ubah tanggal: `ubah tanggal 13/02/2026`")
                    return
                pending.expense_date = new_date
                pending.raw_payload["tanggal"] = format_date_id(new_date)
                service.update_pending_receipt(pending)
                await update.message.reply_text(
                    f"Siap, tanggal diubah jadi {format_date_id(new_date)}. Balas `simpan` atau lanjut ubah."
                )
                return
            if low in {"batal", "tidak", "ga", "gak"}:
                service.clear_pending_receipt(user_key)
                await update.message.reply_text("Oke, struknya tidak jadi disimpan.")
                return
            await update.message.reply_text(
                "Balas `simpan` untuk simpan struk, `batal` untuk batal, "
                "atau `ubah total/kategori/merchant/tanggal ...`."
            )
            return

        response = service.handle_text(user_key, text)
        await update.message.reply_text(response)

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Unhandled error while processing update", exc_info=context.error)

    async def auth_middleware_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user:
            return
        allowed_users = get_settings().allowed_telegram_users
        if allowed_users and update.effective_user.id not in allowed_users:
            if update.message:
                await update.message.reply_text("Maaf, kamu tidak memiliki akses untuk menggunakan bot ini.")
            raise context.application.stop_propagation()

    application.add_handler(MessageHandler(filters.ALL, auth_middleware_handler), group=-1)

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("total", total_handler))
    application.add_handler(CommandHandler("total_hari_ini", total_hari_ini_handler))
    application.add_handler(CommandHandler("total_minggu", total_minggu_handler))
    application.add_handler(CommandHandler("total_bulan", total_bulan_handler))
    application.add_handler(CommandHandler("list", list_handler))
    application.add_handler(CommandHandler("hapus", hapus_handler))
    application.add_handler(CommandHandler("reset", reset_handler))
    application.add_handler(CommandHandler("budget", budget_handler))
    application.add_handler(CommandHandler("grafik", grafik_handler))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_error_handler(error_handler)
    return application


def _user_key(update: Update) -> str:
    user = update.effective_user
    return f"tg:{user.id}" if user else "tg:unknown"


def _build_command_text(command: str, args: list[str]) -> str:
    return f"/{command} {' '.join(args)}".strip()


async def _reply_chunks(message, chunks: list[str]) -> None:
    for chunk in chunks:
        await message.reply_text(chunk)
