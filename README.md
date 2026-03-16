# CuanBot Telegram (Serverless / Containers)

CuanBot adalah chatbot Telegram untuk mencatat pengeluaran, budgeting, split bill, scan struk dengan Florence-2, dan mengirim grafik pengeluaran bulanan sebagai file gambar.

## Arsitektur Baru
- Runtime: FastAPI + webhook Telegram.
- Deployment: Hugging Face Spaces (Docker) atau Vercel Serverless Function.
- Database: PostgreSQL dengan `psycopg_pool`.
- OCR struk: Florence-2 (`microsoft/Florence-2-base`) melalui endpoint Hugging Face eksternal.
- Chart: QuickChart API.

## Struktur Folder
```text
.
|-- app.py
|-- main.py
|-- telegram_bot.py
|-- Dockerfile
|-- vercel.json
|-- requirements.txt
|-- .env.example
`-- expense_bot
    |-- __init__.py
    |-- charts.py
    |-- config.py
    |-- db.py
    |-- ocr.py
    |-- parser.py
    |-- service.py
    `-- telegram_app.py
```

## Environment Variables
Isi `.env` lokal, Hugging Face Spaces Secrets, atau Vercel Settings dengan:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require
BOT_TIMEZONE=Asia/Jakarta
TELEGRAM_BOT_TOKEN=ISI_DARI_BOTFATHER
TELEGRAM_WEBHOOK_SECRET=secret-telegram-webhook
WEBHOOK_SETUP_SECRET=secret-untuk-setup-webhook
PUBLIC_BASE_URL=https://huggingface.co/spaces/username/namaspace
FLORENCE_ENDPOINT_URL=https://endpoint-anda.huggingface.cloud
HUGGINGFACE_API_TOKEN=hf_xxx
FLORENCE_MODEL_ID=microsoft/Florence-2-base
QUICKCHART_URL=https://quickchart.io/chart
ALLOWED_TELEGRAM_USERS=1234567,9876543
```

Catatan:
- `DATABASE_URL` wajib. SQLite lokal lama tidak dipakai lagi untuk deployment baru.
- `ALLOWED_TELEGRAM_USERS` opsional. Isi dengan ID angka Telegram (dipisah koma) untuk membatasi siapa yang bisa memakai bot. Jika kosong, bot terbuka untuk publik.
- `FLORENCE_ENDPOINT_URL` disarankan berupa Hugging Face Inference Endpoint atau service eksternal yang menjalankan `microsoft/Florence-2-base`.
- Endpoint Florence diharapkan menerima JSON:

```json
{
  "model": "microsoft/Florence-2-base",
  "task_prompt": "<OCR>",
  "image_base64": "..."
}
```

- Endpoint Florence diharapkan mengembalikan salah satu bentuk berikut:

```json
{"text": "RAW OCR TEXT"}
```

atau

```json
{"generated_text": "RAW OCR TEXT"}
```

## Jalankan Lokal
```bat
cd /d d:\chatbot
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
copy .env.example .env
python main.py
```

Server lokal jalan di `http://127.0.0.1:8000`.

## Endpoint Aplikasi
- `GET /health`
- `POST /telegram/webhook`
- `GET /telegram/webhook-info`
- `POST /telegram/setup-webhook`
- `DELETE /telegram/webhook`

Untuk endpoint setup/info/delete, kirim header:

```text
X-Setup-Secret: <WEBHOOK_SETUP_SECRET>
```

## Setup Webhook Telegram
Setelah deploy ke Vercel, panggil:

```bash
curl -X POST https://nama-project.vercel.app/telegram/setup-webhook ^
  -H "X-Setup-Secret: WEBHOOK_SETUP_SECRET_ANDA"
```

Webhook akan diarahkan ke:

```text
https://huggingface.co/spaces/username/namaspace/telegram/webhook
```

Jika `TELEGRAM_WEBHOOK_SECRET` terisi, Telegram harus mengirim header `X-Telegram-Bot-Api-Secret-Token` yang cocok.

## Command Bot
- `/help`
- `/total`
- `/total_hari_ini`
- `/total_minggu`
- `/total_bulan`
- `/list` atau `/list 20`
- `/grafik`
- `/budget`
- `/budget 2500000`
- `/budget kategori Makanan & Minuman 700000`
- `/hapus <id>`
- `/reset ya`

Perintah lama `laporan minggu ini` dan `laporan bulan ini` sudah dihapus.

## Perubahan Query
- `/total_hari_ini` mengambil semua transaksi dengan `expense_date = CURRENT_DATE`.
- `/total_minggu` mengambil semua transaksi dari Senin minggu ini sampai hari ini.
- `/total_bulan` mengambil semua transaksi dari tanggal 1 bulan berjalan sampai hari ini.
- Tidak ada limit row di tiga command tersebut.

## Florence-2 Scan Struk
Alur scan:
1. User kirim foto struk.
2. Bot kirim gambar ke endpoint Florence-2.
3. Hasil OCR dinormalisasi ke JSON:

```json
{
  "item": "Belanja Indomaret",
  "total": 125000,
  "tanggal": "14/03/2026",
  "kategori": "Belanja Bulanan"
}
```

4. Bot menyimpan hasil ke tabel `pending_receipts`.
5. User balas `simpan` atau `ubah total/kategori/merchant/tanggal ...`.

State OCR tidak lagi disimpan di memory, jadi aman untuk runtime serverless.

## Grafik
Command `/grafik` akan:
- merangkum pengeluaran bulan ini per kategori dari PostgreSQL,
- membuat doughnut chart melalui QuickChart,
- mengirim hasilnya sebagai file PNG ke Telegram.

## Setup Supabase PostgreSQL (Cloud Database)
Karena deployment Vercel bersifat *serverless* dan tidak mendukung file SQLite lokal, sangat disarankan menggunakan Supabase:

1. Buat project gratis di [Supabase.com](https://supabase.com).
2. Buka dashboard project, masuk ke menu **Project Settings** > **Database**.
3. Gulir ke bagian **Connection string**, pilih tab **URI**.
4. Salin URI yang diberikan. Pastikan menggunakan **port 6543** (connection pooling) agar aman dari connection limit di Vercel.
5. Ganti teks `[YOUR-PASSWORD]` dengan password database Anda.
6. Tambahkan parameter `?sslmode=require` di bagian paling akhir URL.

Contoh format `DATABASE_URL` yang benar:
```text
postgresql://postgres.namaproject:PasswordRahasia123@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres?sslmode=require
```

Gunakan URL tersebut untuk dipasang di `.env` lokal atau di Environment Variables konfigurasi server.

## Deploy ke Hugging Face Spaces (Docker)

> [!WARNING]  
> **LIMITASI FREE TIER**: Hugging Face Spaces versi *Free Tier* (Hardware: CPU Basic - Free) **MEMBLOKIR** koneksi *outbound* internet ke luar, termasuk koneksi ke `api.telegram.org` dan database Eksternal Supabase. Inilah penyebab munculnya error `[Errno -5] No address associated with hostname`. 
> 
> Agar bot dapat membalas pesan ke Telegram dan menghubungi Supabase, Anda **WAJIB** meningkatkan/upgrade spesifikasi *Hardware* Space Anda minimal ke spesifikasi berbayar terendah (misal: CPU Upgrade). Hal ini otomatis akan membuka akses internet *outbound*.

1. Buat Space baru di Hugging Face, pilih SDK **Docker** (Blank).
2. Push repository ini (termasuk `Dockerfile`) ke repositori Hugging Face yang baru Anda buat, atau gunakan GitHub Actions `sync_to_huggingface.yml`.
3. Buka tab **Settings** di Halaman Space Anda.
4. Di bagian **Variables and secrets**, tekan **New secret** lalu masukkan semua *Environment Variables* di atas satu per satu (seperti `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, dsb).
5. Pada rahasia `PUBLIC_BASE_URL`, isi URL publik app Anda (misal `https://namasamaran-cuanbot.hf.space` -  Anda dapat melihat public url ini dengan mengeklik *Embed this Space* di kanan atas (Pilih Direct URL)).
6. Hugging Face akan membangun ulang kontainernya secara otomatis.
7. Panggil endpoint setup webhook dari terminal Anda:

```bash
curl -X POST https://namasamaran-cuanbot.hf.space/telegram/setup-webhook ^
  -H "X-Setup-Secret: WEBHOOK_SETUP_SECRET_ANDA"
```

## Catatan Infrastruktur
- Model Florence-2 tidak dijalankan langsung di memori bot agar stabil. Bot memanggil endpoint dari Inference Huggingface.
- Pooling dipakai melalui `psycopg_pool` agar koneksi ke Supabase stabil.
