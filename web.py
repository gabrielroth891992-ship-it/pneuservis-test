import os
import csv
import json
import hmac
import time
import urllib.request
import smtplib
from collections import defaultdict, deque
from threading import Lock
from email.message import EmailMessage
from datetime import date

from flask import Flask, request, jsonify, render_template_string, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
from openai import OpenAI

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
EMAIL_FROM = os.getenv("EMAIL_FROM", "").strip()

EMAIL_OK = all(
    os.getenv(name)
    for name in [
        "EMAIL_SMTP_HOST",
        "EMAIL_SMTP_PORT",
        "EMAIL_USERNAME",
        "EMAIL_PASSWORD",
        "EMAIL_TO",
        "EMAIL_FROM",
    ]
)

if not API_KEY:
    raise RuntimeError("V .env chyba OPENAI_API_KEY.")

client = OpenAI(api_key=API_KEY)
app = Flask(__name__)
# Render sits behind a reverse proxy; use its forwarded client address/protocol.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
TODAY_TEXT = date.today().strftime("%d.%m.%Y")

# Lightweight per-client limit to reduce accidental/API-cost abuse on the public demo.
_RATE_WINDOW_SECONDS = 600
_RATE_MAX_REQUESTS = 30
_rate_hits = defaultdict(deque)
_rate_lock = Lock()

def is_chat_rate_limited(ip_address):
    now = time.monotonic()
    with _rate_lock:
        hits = _rate_hits[ip_address]
        while hits and now - hits[0] >= _RATE_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= _RATE_MAX_REQUESTS:
            return True
        hits.append(now)
        return False

def admin_auth_response():
    username = os.getenv("ADMIN_USERNAME", "")
    password = os.getenv("ADMIN_PASSWORD", "")
    if not username or not password:
        return Response(
            "Admin nie je nakonfigurovaný. Nastav ADMIN_USERNAME a ADMIN_PASSWORD.",
            status=503,
            mimetype="text/plain",
        )

    auth = request.authorization
    valid = (
        auth is not None
        and hmac.compare_digest(auth.username or "", username)
        and hmac.compare_digest(auth.password or "", password)
    )
    if valid:
        return None

    return Response(
        "Na zobrazenie dopytov sa prihlás používateľským menom a heslom.",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Pneuservis Test Admin"'},
        mimetype="text/plain",
    )

SYSTEM_PROMPT = f"""
Si AI recepčný pre Pneuservis Test v Testovove.

INFORMÁCIE O PREVÁDZKE:
- Názov: Pneuservis Test
- Adresa: Testovacia 11, 12349 Testovovo
- Telefón: +421 000 000 000
- Dnešný dátum: {TODAY_TEXT}

TVOJ CIEĽ:
Správaj sa ako schopný pneuservisný recepčný. Pomôž zákazníkovi pochopiť,
akú službu potrebuje, a priprav kvalitný dopyt pre pracovníka pneuservisu.

KOMUNIKÁCIA:
- Komunikuj po slovensky, prirodzene, stručne a profesionálne.
- Pýtaj sa vždy iba JEDNU otázku naraz.
- Ak zákazník uvedie viac údajov naraz, zapamätaj si ich.
- Ak sa zákazník iba informuje, netlač ho do objednávky.
- Nevymýšľaj si ceny, skladovú dostupnosť, značky ani fakty o prevádzke.

SLUŽBY V DEME:
Vieš vybavovať dopyty týkajúce sa:
- sezónneho prezúvania,
- výmeny kompletných kolies,
- výmeny pneumatík na diskoch,
- vyváženia kolies,
- opravy defektu ako požiadavky na posúdenie pracovníkom,
- dopytov na nové letné, zimné a celoročné pneumatiky.

Toto je demo ponuky, nie potvrdenie reálnej dostupnosti. Ak sa zákazník pýta
na službu mimo uvedeného zoznamu, povedz, že ju nemáš potvrdenú a musí ju
overiť pracovník Pneuservisu Test.

NOVÉ PNEUMATIKY:
Ak zákazník chce kúpiť pneumatiky, postupne zisťuj podľa potreby:
- sezónu: letné, zimné alebo celoročné,
- rozmer, ak ho pozná (napr. 205/55 R16),
- počet kusov (napr. 2 alebo 4),
- či chce aj prezutie/montáž,
- prípadnú preferenciu značky alebo cenovej triedy.
Nepýtaj sa na všetko naraz. Ak zákazník nepozná rozmer, netlač naňho;
môže ho skontrolovať na bočnici pneumatiky alebo ho pracovník pomôže zistiť.

CENY A SKLAD:
- Nikdy si nevymýšľaj cenu.
- Nikdy netvrď, že konkrétny rozmer alebo značka je skladom.
- Ak nemáš overený cenník alebo skladové údaje, povedz, že cenu a dostupnosť
  musí potvrdiť pracovník.
- Nesľubuj orientačný cenový odhad bez reálnych údajov.

TERMÍNY:
- Nikdy netvrď, že termín je voľný alebo rezervovaný.
- Zapisuj iba preferovaný dátum a voliteľný čas zákazníka.
- Definitívny termín musí potvrdiť pracovník Pneuservisu Test.
- Výrazy dnes, zajtra, pozajtra, deň v týždni alebo "o 3 dni" preveď
  na dátum podľa dnešného dátumu {TODAY_TEXT}.
- Časy "17.20", "17:20", "17 20" alebo "o tretej" ber ako čas.
- Ak zákazník uvedie iba čas bez dátumu, opýtaj sa na dátum.

BEZPEČNOSŤ:
Ak zákazník opisuje poškodenú pneumatiku, vydutie bočnice, rýchly únik tlaku,
uvoľnené koleso alebo inú možnú bezpečnostnú poruchu, neoznačuj jazdu za
bezpečnú. Odporuč bezpečne zastaviť a kontaktovať odborníka, ak je to potrebné.

ÚDAJE PRE DOPYT:
Pri pneuservisnom dopyte postupne zisti:
1. vozidlo - značka, model a ideálne rok,
2. požiadavku (zachyť aj rozmer/počet kusov/sezónu, ak sú relevantné),
3. preferovaný dátum,
4. preferovaný čas (voliteľný),
5. meno,
6. telefónne číslo.

VIN ani EČV nežiadaj automaticky. Ak zákazník sám uvedie poznámku,
EČV, VIN alebo zvláštnu požiadavku, môžeš ju zaznamenať.

TELEFÓN:
- Prijmi slovenské aj zahraničné čísla.
- Medzery, pomlčky a znak + sú v poriadku.
- Číslo svojvoľne neupravuj.
- Ak vyzerá očividne nezmyselne, slušne požiadaj o kontrolu.

DOPYT JE KOMPLETNÝ, keď máš:
- meno,
- telefón,
- vozidlo,
- požiadavku,
- preferovaný dátum.

ČAS a POZNÁMKA sú voliteľné.

Keď sú povinné údaje kompletné, zákazníkovi najprv odpovedz normálnou
ľudskou vetou. Potom pridaj interný blok, ktorý systém skryje:

LEAD_READY
MENO: ...
TELEFON: ...
VOZIDLO: ...
POZIADAVKA: ...
TERMIN: ...
CAS: ...
POZNAMKA: ...

CAS a POZNAMKA môžu byť prázdne.
Nikdy netvrď, že termín je definitívne rezervovaný.
"""


def extract_lead(answer):
    data = {}
    for line in answer.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in [
            "MENO",
            "TELEFON",
            "VOZIDLO",
            "POZIADAVKA",
            "TERMIN",
            "CAS",
            "POZNAMKA",
        ]:
            data[key] = value

    required = ["MENO", "TELEFON", "VOZIDLO", "POZIADAVKA", "TERMIN"]
    if not all(key in data for key in required):
        return None

    data.setdefault("CAS", "")
    data.setdefault("POZNAMKA", "")
    return data


def clean_customer_answer(answer):
    if "LEAD_READY" not in answer:
        return answer.strip()

    visible = answer.split("LEAD_READY", 1)[0].strip()
    if visible:
        return visible

    return (
        "Ďakujem. Váš pneuservisný dopyt som pripravil. "
        "Pracovník Pneuservisu Test vás bude kontaktovať "
        "kvôli potvrdeniu ceny a termínu."
    )


def get_database_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL nie je nastavený.")
    if psycopg is None:
        raise RuntimeError("Chýba balík psycopg. Nainštaluj requirements.txt.")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)


def _insert_lead_database(connection, data):
    connection.execute(
        """
        INSERT INTO tire_service_leads
            (name, phone, vehicle, requirement, preferred_date, preferred_time, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            data.get("MENO", ""),
            data.get("TELEFON", ""),
            data.get("VOZIDLO", ""),
            data.get("POZIADAVKA", ""),
            data.get("TERMIN", ""),
            data.get("CAS", ""),
            data.get("POZNAMKA", ""),
        ),
    )


def _read_csv_leads():
    path = "pneuservis_leads.csv"
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def init_database():
    """Create the table and import existing CSV rows once if the database is empty."""
    with get_database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tire_service_leads (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                vehicle TEXT NOT NULL,
                requirement TEXT NOT NULL,
                preferred_date TEXT NOT NULL,
                preferred_time TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        count_row = connection.execute(
            "SELECT COUNT(*) AS total FROM tire_service_leads"
        ).fetchone()
        database_is_empty = count_row["total"] == 0

        # Best-effort one-time migration: only import if the persistent database is empty.
        if database_is_empty:
            old_rows = _read_csv_leads()
            imported = 0
            for row in old_rows:
                if not row.get("Meno") or not row.get("Telefon") or not row.get("Vozidlo"):
                    continue
                data = {
                    "MENO": row.get("Meno", ""),
                    "TELEFON": row.get("Telefon", ""),
                    "VOZIDLO": row.get("Vozidlo", ""),
                    "POZIADAVKA": row.get("Poziadavka", ""),
                    "TERMIN": row.get("Termin", ""),
                    "CAS": row.get("Cas", ""),
                    "POZNAMKA": row.get("Poznamka", ""),
                }
                if data["POZIADAVKA"] and data["TERMIN"]:
                    _insert_lead_database(connection, data)
                    imported += 1
            if imported:
                print(f"PostgreSQL: importovaných {imported} starších dopytov z pneuservis_leads.csv.")
        connection.commit()
    print("PostgreSQL databáza pripravená.")


def save_lead(data):
    if DATABASE_URL:
        with get_database_connection() as connection:
            _insert_lead_database(connection, data)
            connection.commit()
        print("Dopyt uložený do PostgreSQL.")
        return

    # Local-development fallback when DATABASE_URL is not configured.
    path = "pneuservis_leads.csv"
    new_header = ["Meno", "Telefon", "Vozidlo", "Poziadavka", "Termin", "Cas", "Poznamka"]
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8-sig") as file:
            csv.writer(file).writerow(new_header)

    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))

    if rows and rows[0] != new_header:
        converted = []
        for row in rows[1:]:
            row = list(row)
            while len(row) < 5:
                row.append("")
            converted.append(row[:5] + ["", ""])
        with open(path, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(new_header)
            writer.writerows(converted)

    with open(path, "a", newline="", encoding="utf-8-sig") as file:
        csv.writer(file).writerow([
            data["MENO"], data["TELEFON"], data["VOZIDLO"],
            data["POZIADAVKA"], data["TERMIN"], data.get("CAS", ""),
            data.get("POZNAMKA", ""),
        ])


def load_leads():
    if DATABASE_URL:
        with get_database_connection() as connection:
            rows = connection.execute(
                """
                SELECT name, phone, vehicle, requirement, preferred_date,
                       preferred_time, note
                FROM tire_service_leads
                ORDER BY id DESC
                LIMIT 200
                """
            ).fetchall()
        return [
            {
                "Meno": row["name"],
                "Telefon": row["phone"],
                "Vozidlo": row["vehicle"],
                "Poziadavka": row["requirement"],
                "Termin": row["preferred_date"],
                "Cas": row["preferred_time"],
                "Poznamka": row["note"],
            }
            for row in rows
        ]
    return _read_csv_leads()


def format_lead_message(data):
    message = (
        "🛞 NOVÝ DOPYT – PNEUSERVIS TEST\n\n"
        f"👤 Meno: {data['MENO']}\n"
        f"📞 Telefón: {data['TELEFON']}\n"
        f"🚙 Vozidlo: {data['VOZIDLO']}\n"
        f"🛞 Požiadavka: {data['POZIADAVKA']}\n"
        f"📅 Preferovaný dátum: {data['TERMIN']}"
    )

    if data.get("CAS", "").strip():
        message += f"\n🕐 Preferovaný čas: {data['CAS']}"

    if data.get("POZNAMKA", "").strip():
        message += f"\n📝 Poznámka: {data['POZNAMKA']}"

    message += "\n\n⚠️ Termín ešte nie je potvrdený."
    return message


def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        print("Telegram nie je nastavený - preskakujem.")
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TG_CHAT, "text": message}).encode("utf-8")
    request_obj = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            response.read()
        print("Telegram upozornenie odoslané.")
    except Exception as error:
        print("Telegram chyba:", error)


def send_email(subject, body):
    if not EMAIL_OK:
        print("E-mail nie je nastavený - preskakujem.")
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"Pneuservis Test <{EMAIL_FROM}>"
    message["To"] = os.getenv("EMAIL_TO")
    message.set_content(body)

    try:
        with smtplib.SMTP(
            os.getenv("EMAIL_SMTP_HOST"),
            int(os.getenv("EMAIL_SMTP_PORT")),
            timeout=15,
        ) as smtp:
            smtp.starttls()
            smtp.login(
                os.getenv("EMAIL_USERNAME"),
                os.getenv("EMAIL_PASSWORD"),
            )
            smtp.send_message(message)
        print("E-mail upozornenie odoslané.")
    except Exception as error:
        print("E-mail chyba:", error)


def extract_lead_with_ai(conversation):
    """Recover a completed lead when the main reply omitted LEAD_READY."""
    extractor_prompt = """
Z celej konverzácie vyťaž pneuservisný dopyt. Použi iba údaje, ktoré sa v konverzácii
skutočne nachádzajú. Ak chýba niektorý povinný údaj, vráť iba FAIL.
Ak sú všetky povinné údaje dostupné, vráť IBA tento blok:

LEAD_READY
MENO: ...
TELEFON: ...
VOZIDLO: ...
POZIADAVKA: ...
TERMIN: ...
CAS: ...
POZNAMKA: ...

Povinné sú MENO, TELEFON, VOZIDLO, POZIADAVKA a TERMIN.
CAS a POZNAMKA môžu byť prázdne. Telefón zachovaj presne tak, ako ho zákazník zadal.
"""
    try:
        response = client.responses.create(
            model="gpt-5-mini",
            instructions=extractor_prompt,
            input=conversation,
        )
        text = response.output_text.strip()
        if text.startswith("FAIL"):
            return None
        if "LEAD_READY" in text:
            return extract_lead(text)
    except Exception as error:
        print("EXTRAKCIA LEADU CHYBA:", error)
    return None


def notify_new_lead(data):
    message = format_lead_message(data)
    save_lead(data)
    send_telegram(message)
    send_email("Nový dopyt - Pneuservis Test", message)


CHAT_HTML = r'''<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pneuservis Test</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:Arial,Helvetica,sans-serif;background:radial-gradient(circle at top left,#334155 0,transparent 35%),linear-gradient(135deg,#0f172a,#111827);min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}.container{width:100%;max-width:920px}.brand{text-align:center;color:#fff;margin-bottom:18px}.brand h1{margin:0;font-size:34px}.brand p{margin:8px 0;color:#cbd5e1}.chat{height:700px;background:#fff;border-radius:24px;overflow:hidden;box-shadow:0 30px 80px rgba(0,0,0,.4);display:flex;flex-direction:column}.header{background:#111827;color:#fff;padding:20px 24px;display:flex;justify-content:space-between;align-items:center;gap:15px}.header h2{margin:0;font-size:21px}.header span{display:block;margin-top:6px;color:#86efac;font-size:13px}.admin{background:#374151;color:#fff;text-decoration:none;padding:10px 14px;border-radius:10px;font-size:13px;white-space:nowrap}.messages{flex:1;padding:26px;overflow-y:auto;background:#f8fafc}.message{max-width:78%;margin-bottom:16px;padding:14px 17px;border-radius:17px;line-height:1.55;white-space:pre-wrap;font-size:15px}.ai{background:#fff;color:#1f2937;margin-right:auto;border:1px solid #e5e7eb}.user{background:#111827;color:#fff;margin-left:auto}.input-area{display:flex;gap:10px;padding:16px;background:#fff;border-top:1px solid #e5e7eb}input{flex:1;border:1px solid #d1d5db;border-radius:12px;padding:14px;font-size:15px;outline:none}input:focus{border-color:#111827}button{border:none;background:#111827;color:#fff;padding:0 23px;border-radius:12px;cursor:pointer;font-size:15px;font-weight:bold}button:hover{background:#374151}.footer{text-align:center;color:#94a3b8;font-size:12px;margin-top:14px}@media(max-width:700px){body{padding:10px}.chat{height:88vh;border-radius:18px}.message{max-width:88%}.brand h1{font-size:27px}}
</style></head>
<body><div class="container"><div class="brand"><h1>Pneuservis Test</h1><p>Váš inteligentný pneuservisný asistent</p></div><div class="chat"><div class="header"><div><h2>🛞 Pneuservis Test</h2><span>● Online asistent</span></div><a class="admin" href="/admin">📋 Dopyty</a></div><div class="messages" id="messages"><div class="message ai">Dobrý deň 👋

Vitajte v Pneuservise Test.

Môžem vám pomôcť s prezutím, výmenou pneumatík, vyvážením kolies alebo pripraviť dopyt na novú sadu pneumatík.

Ako vám môžem pomôcť?</div></div><div class="input-area"><input id="messageInput" type="text" placeholder="Napíšte svoju požiadavku..." autocomplete="off"><button onclick="sendMessage()">Odoslať</button></div></div><div class="footer">Pneuservis Test · Testovovo · +421 000 000 000</div></div>
<script>
let conversation=[];
function addMessage(text,type){const messages=document.getElementById('messages');const div=document.createElement('div');div.className='message '+type;div.innerText=text;messages.appendChild(div);messages.scrollTop=messages.scrollHeight;}
async function sendMessage(){const input=document.getElementById('messageInput');const message=input.value.trim();if(!message)return;addMessage(message,'user');input.value='';conversation.push({role:'user',content:message});try{const response=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conversation:conversation})});const data=await response.json();if(!response.ok)throw new Error(data.error||'Server error');addMessage(data.answer,'ai');conversation.push({role:'assistant',content:data.answer});}catch(error){addMessage('Prepáčte, momentálne sa nepodarilo spojiť so serverom.','ai');}}
document.getElementById('messageInput').addEventListener('keydown',function(event){if(event.key==='Enter')sendMessage();});
</script></body></html>'''


ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pneuservis Test - Dopyty</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:Arial,Helvetica,sans-serif;background:#f2f4f7;padding:30px}.container{max-width:1100px;margin:auto}.top{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:25px}h1{color:#111827;margin:0}.back{background:#111827;color:#fff;padding:11px 16px;border-radius:9px;text-decoration:none;white-space:nowrap}.lead{background:#fff;padding:24px;margin-bottom:16px;border-radius:15px;box-shadow:0 5px 18px rgba(0,0,0,.08)}.lead h2{margin-top:0;color:#111827}.row{margin:11px 0;font-size:16px}.label{font-weight:bold;display:inline-block;width:145px}.empty{background:#fff;padding:35px;border-radius:14px;text-align:center;color:#64748b}@media(max-width:700px){body{padding:12px}.top{flex-direction:column;align-items:flex-start}.label{display:block;width:auto;margin-bottom:3px}}
</style></head>
<body><div class="container"><div class="top"><h1>🛞 Pneuservis Test — Dopyty</h1><a class="back" href="/">← Späť na chat</a></div>
{% if leads %}{% for lead in leads %}<div class="lead"><h2>📩 Zákaznícky dopyt</h2><div class="row"><span class="label">Meno:</span>{{ lead.get("Meno", "") }}</div><div class="row"><span class="label">Telefón:</span>{{ lead.get("Telefon", "") }}</div><div class="row"><span class="label">Vozidlo:</span>{{ lead.get("Vozidlo", "") }}</div><div class="row"><span class="label">Požiadavka:</span>{{ lead.get("Poziadavka", "") }}</div><div class="row"><span class="label">Dátum:</span>{{ lead.get("Termin", "") }}</div>{% if lead.get("Cas", "") %}<div class="row"><span class="label">Čas:</span>{{ lead.get("Cas", "") }}</div>{% endif %}{% if lead.get("Poznamka", "") %}<div class="row"><span class="label">Poznámka:</span>{{ lead.get("Poznamka", "") }}</div>{% endif %}</div>{% endfor %}{% else %}<div class="empty">Zatiaľ nemáš žiadne dopyty.</div>{% endif %}</div></body></html>'''


@app.route("/")
def home():
    return render_template_string(CHAT_HTML)


@app.route("/chat", methods=["POST"])
def chat():
    try:
        if is_chat_rate_limited(request.remote_addr or "unknown"):
            return jsonify({"error": "Dosiahli ste limit správ. Počkajte niekoľko minút a skúste znova."}), 429

        data = request.get_json(silent=True) or {}
        incoming = data.get("conversation", [])
        if not isinstance(incoming, list):
            return jsonify({"error": "Neplatný formát správy."}), 400

        # Keep only a short, sane user/assistant transcript to limit misuse and cost.
        conversation = []
        for item in incoming[-20:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            content = content[:3000]
            conversation.append({"role": role, "content": content})

        response = client.responses.create(
            model="gpt-5-mini",
            instructions=SYSTEM_PROMPT,
            input=conversation,
        )

        raw_answer = response.output_text

        lead = None
        if "LEAD_READY" in raw_answer:
            lead = extract_lead(raw_answer)
        else:
            # The model can occasionally finish the customer-facing reply without
            # emitting the internal marker. Recover the lead from the transcript.
            low = raw_answer.lower()
            looks_complete = (
                "dopyt" in low
                and ("pripravil" in low or "pripraven" in low or "zaznamenal" in low)
                and ("telef" in low)
                and ("term" in low)
            )
            if looks_complete:
                lead = extract_lead_with_ai(conversation)

        if lead:
            notify_new_lead(lead)

        return jsonify({"answer": clean_customer_answer(raw_answer)})

    except Exception as error:
        print("CHYBA:", error)
        return jsonify({"error": "Chyba servera. Skontroluj terminál."}), 500


@app.route("/admin")
def admin():
    auth_failure = admin_auth_response()
    if auth_failure is not None:
        return auth_failure

    try:
        leads = load_leads()
    except Exception as error:
        print("ADMIN DOPYTY CHYBA:", error)
        return Response(
            "Dopyty sa momentálne nepodarilo načítať. Skontroluj databázové nastavenia.",
            status=503,
            mimetype="text/plain",
        )

    return render_template_string(ADMIN_HTML, leads=leads)


# On Render, require a persistent database rather than silently using ephemeral CSV.
if os.getenv("RENDER_EXTERNAL_URL") and not DATABASE_URL:
    raise RuntimeError("Na Renderi nastav environment variable DATABASE_URL na Neon connection string.")

if DATABASE_URL:
    init_database()


if __name__ == "__main__":
    print()
    print("====================================")
    print("         PNEUSERVIS TEST")
    print("====================================")
    print()
    print("Chat:  http://127.0.0.1:5000")
    print("Admin: http://127.0.0.1:5000/admin")
    print()
    print("Pre ukončenie stlač CTRL+C")
    print()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
