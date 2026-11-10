"""
Exam Viewer — lisenziya yoxlaması.

* İstifadəçi proqramı yalnız lisenziya açarı ilə aça bilər. 1 açar = 1 kompüter.
* Açarlar Firebase Firestore-da saxlanılır: examviewer_licenses/{AÇAR}
  Sahələr: note, active, expiresAt, deviceId, deviceName, activatedAt, lastSeen, createdAt
* Yoxlama Python tərəfdə aparılır: açar təsdiqlənməyibsə API (/exams, /jobs, ...) 403 qaytarır,
  ona görə viewer.html-ı dəyişməklə lisenziyadan yan keçmək olmur.
* İnternet olmayanda son uğurlu yoxlamadan sonra OFFLINE_GRACE_DAYS gün işləyir.
* Admin Firebase e-poçt/parol ilə daxil olur (viewer.html); ID token burada Google vasitəsilə yoxlanılır.
* Əlavə paket lazım deyil — yalnız Python standart kitabxanası (+ artıq qurulu fastapi/pydantic).
"""
import hashlib
import hmac
import secrets
import json
import platform
import re
import socket
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

# ---------------------------------------------------------------- ayarlar
FIREBASE_CONFIG = {
    "apiKey": "AIzaSyCLUatP2m1Z11FbgjIO8D_oDHRUUE708vg",
    "authDomain": "satis-reyestri.firebaseapp.com",
    "projectId": "satis-reyestri",
    "storageBucket": "satis-reyestri.firebasestorage.app",
    "messagingSenderId": "167545350718",
    "appId": "1:167545350718:web:54c08a362ae14010773f6f",
}
ADMIN_EMAIL = "admin@admin.com"            # firestore.rules-dakı ilə eyni olmalıdır

# ---------------------------------------------------------------- satış
# Lisenziya ekranında göstərilir: tələbə tarifi seçir, pulu karta köçürür, qəbzi e-poçta göndərir,
# admin açarı yaradıb e-poçtla göndərir. Qiyməti və ya kartı dəyişmək üçün yalnız buranı dəyiş.
SALES = {
    "email": "cekdaniels260@gmail.com",     # qəbzlər bu ünvana göndərilir
    "card": "5315 9921 2754 4077",          # pul bu karta köçürülür
    "currency": "AZN",
    "plans": [                               # "days" admin paneldə açar yaradanda istifadə olunur
        {"id": "1m", "title": "1 ay", "label": "1 aylıq", "days": 30, "price": 1.00},
        {"id": "3m", "title": "3 ay", "label": "3 aylıq", "days": 90, "price": 2.50},
        {"id": "6m", "title": "6 ay", "label": "6 aylıq", "days": 180, "price": 4.00},
        {"id": "12m", "title": "1 il", "label": "1 illik", "days": 365, "price": 7.00},
    ],
}
LICENSE_COLLECTION = "examviewer_licenses"
OFFLINE_GRACE_DAYS = 7                     # internetsiz neçə gün işləsin
HTTP_TIMEOUT = 8                           # saniyə

FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_CONFIG['projectId']}/databases/(default)/documents"
)
LOOKUP_URL = "https://identitytoolkit.googleapis.com/v1/accounts:lookup"

KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 0/O və 1/I yoxdur — səhv oxunmasın
_KEY_RE = re.compile(f"^[{KEY_ALPHABET}]{{20}}$")
_STATE_SECRET = b"ExamViewer|license-state|v1"

MESSAGES = {
    "bad_format": "Açarın formatı səhvdir. Nümunə: ABCDE-FGHJK-LMNPQ-RSTUV",
    "not_found": "Bu lisenziya açarı tapılmadı. Açarı yoxla.",
    "blocked": "Bu lisenziya bloklanıb. Adminə müraciət et.",
    "expired": "Lisenziyanın müddəti bitib ({date}). Adminə müraciət et.",
    "other_device": "Bu açar artıq başqa kompüterdə aktivləşdirilib. Adminə müraciət et.",
    "rules": "Firebase qaydaları qurulmayıb (Firestore → Rules → Exam Viewer bloku).",
    "offline": "İnternet bağlantısı yoxdur. Açarı aktivləşdirmək üçün internet lazımdır.",
    "offline_expired": f"Lisenziyanı yoxlamaq üçün internetə qoşul (son {OFFLINE_GRACE_DAYS} gündə yoxlanmayıb).",
    "server": "Lisenziya serveri cavab vermədi ({detail}). Bir az sonra yenidən yoxla.",
    "ssl": "Təhlükəsiz bağlantı qurulmadı (SSL sertifikatı). Kompüterin tarix və saatını yoxla.",
}


# Hər açılışda təsadüfi sessiya tokeni. Proqramın öz pəncərəsi onu kuki kimi daşıyır;
# kənar skript və ya başqa proqram bu tokeni bilmədən yazma (POST) əməliyyatı edə bilmir.
SESSION_TOKEN = secrets.token_urlsafe(24)
SESSION_COOKIE = "ev_session"


# ---------------------------------------------------------------- kompüter
def _machine_guid() -> str:
    """Kompüterin sabit ID-si: Windows → MachineGuid, macOS → IOPlatformUUID, Linux → machine-id.
    Hostname-ə bağlı deyil (Mac-də hostname şəbəkəyə görə dəyişə bilir)."""
    if sys.platform == "win32":
        try:
            import winreg  # type: ignore

            flags = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, flags) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
                if value:
                    return str(value)
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            out = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5).stdout
            found = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{16,})"', out)
            if found:
                return found.group(1).upper()
        except Exception:
            pass
    else:
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                value = Path(path).read_text(encoding="utf-8").strip()
                if value:
                    return value
            except Exception:
                pass
    return f"{platform.node()}|{uuid.getnode():012x}"


def _device_name() -> str:
    name = ""
    if sys.platform == "darwin":
        try:   # Mac-in "Computer Name"-i (System Settings → General → Sharing)
            name = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            name = ""
    name = name or re.sub(r"\.local$", "", platform.node() or "")
    return (name or "Kompüter")[:60]


DEVICE_ID = hashlib.sha256(("examviewer-device|" + _machine_guid()).encode("utf-8")).hexdigest()[:32]
DEVICE_NAME = _device_name()


def _ssl_context() -> ssl.SSLContext:
    """Sistem sertifikatları + certifi. Mac-də yığılmış Python sistem sertifikatlarını görmür,
    certifi olmasa lisenziya yoxlaması "internet yoxdur" kimi uğursuz olardı."""
    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return context


_SSL_CONTEXT = _ssl_context()


# ---------------------------------------------------------------- köməkçilər
class LicenseError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class OfflineError(Exception):
    pass


def normalize_key(raw: str) -> Optional[str]:
    key = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    if not _KEY_RE.match(key):
        return None
    return "-".join(key[i:i + 5] for i in range(0, 20, 5))


def mask_key(key: str) -> str:
    parts = key.split("-")
    return f"{parts[0]}-•••••-•••••-{parts[-1]}" if len(parts) == 4 else "•••••"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    value = str(text).strip().replace("Z", "+00:00")
    # Firestore nanosaniyə qaytarır; köhnə Python yalnız 6 rəqəmi başa düşür
    value = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], value)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json(raw: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw or b"{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _server_time(headers) -> datetime:
    """Vaxtı Google serverinin cavabından götürürük — kompüterin saatını geri çəkmək kömək etməsin."""
    try:
        value = headers.get("Date") if headers is not None else None
        if value:
            dt = parsedate_to_datetime(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return _utcnow()


def _http(method: str, url: str, params=None, body=None) -> Tuple[int, Dict[str, Any], datetime]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT, context=_SSL_CONTEXT) as resp:
            return resp.status, _json(resp.read()), _server_time(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            payload = _json(exc.read())
        except Exception:
            payload = {}
        return exc.code, payload, _server_time(exc.headers)
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(exc, ssl.SSLCertVerificationError):
            raise LicenseError("ssl", MESSAGES["ssl"]) from exc
        raise OfflineError(str(exc)) from exc


def _error_detail(payload: Dict[str, Any], status: int) -> str:
    err = payload.get("error") or {}
    return str(err.get("status") or err.get("message") or f"HTTP {status}")


def _from_value(value: Any) -> Any:
    if not isinstance(value, dict) or "nullValue" in value:
        return None
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "timestampValue" in value:
        return _parse_iso(value["timestampValue"])
    return None


def _to_value(value: Any) -> Dict[str, Any]:
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, datetime):
        return {"timestampValue": _iso(value)}
    return {"stringValue": str(value)}


# ---------------------------------------------------------------- Firestore
def _doc_url(key: str) -> str:
    return f"{FIRESTORE_BASE}/{LICENSE_COLLECTION}/{urllib.parse.quote(key)}"


def fetch_license(key: str) -> Tuple[Optional[Dict[str, Any]], datetime]:
    status, payload, now = _http("GET", _doc_url(key), params={"key": FIREBASE_CONFIG["apiKey"]})
    if status == 404:
        return None, now
    if status == 403:
        raise LicenseError("rules", MESSAGES["rules"])
    if status != 200:
        raise LicenseError("server", MESSAGES["server"].format(detail=_error_detail(payload, status)))
    doc = {name: _from_value(value) for name, value in (payload.get("fields") or {}).items()}
    doc["_updateTime"] = payload.get("updateTime")
    return doc, now


def _patch(key: str, values: Dict[str, Any], update_time: Optional[str] = None):
    params: List[Tuple[str, str]] = [("key", FIREBASE_CONFIG["apiKey"])]
    params += [("updateMask.fieldPaths", name) for name in values]
    if update_time:
        params.append(("currentDocument.updateTime", update_time))
    body = {"fields": {name: _to_value(value) for name, value in values.items()}}
    return _http("PATCH", _doc_url(key), params=params, body=body)


def _validate(doc: Optional[Dict[str, Any]], now: datetime) -> None:
    if doc is None:
        raise LicenseError("not_found", MESSAGES["not_found"])
    if doc.get("active") is False:
        raise LicenseError("blocked", MESSAGES["blocked"])
    expires = doc.get("expiresAt")
    if isinstance(expires, datetime) and expires <= now:
        raise LicenseError("expired", MESSAGES["expired"].format(date=expires.strftime("%d.%m.%Y")))
    device = doc.get("deviceId")
    if device and device != DEVICE_ID:
        raise LicenseError("other_device", MESSAGES["other_device"])


# ---------------------------------------------------------------- yerli fayl
class _StateFile:
    """Aktivləşdirilmiş açar bu kompüterdə saxlanılır. İmzalıdır və yalnız bu kompüterdə keçərlidir."""

    def __init__(self, path: Path):
        self.path = path

    def _sign(self, text: str) -> str:
        return hmac.new(_STATE_SECRET + DEVICE_ID.encode(), text.encode("utf-8"), hashlib.sha256).hexdigest()

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            text = raw["payload"]
            if not hmac.compare_digest(str(raw.get("sig", "")), self._sign(text)):
                return None
            data = json.loads(text)
            return data if data.get("deviceId") == DEVICE_ID and normalize_key(data.get("key", "")) else None
        except Exception:
            return None

    def save(self, data: Dict[str, Any]) -> None:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"payload": text, "sig": self._sign(text)}), encoding="utf-8")
        tmp.replace(self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- əsas sinif
class LicenseManager:
    def __init__(self, data_dir: Path):
        self.state = _StateFile(Path(data_dir) / "license.dat")
        self.lock = threading.RLock()
        self.licensed = False
        self.admin = False
        self.session_token = SESSION_TOKEN

    def allowed(self) -> bool:
        return self.licensed or self.admin

    # --- cavab formaları
    def _ok(self, data: Dict[str, Any], offline: bool = False) -> Dict[str, Any]:
        self.licensed = True
        return {
            "licensed": True, "admin": self.admin, "offline": offline,
            "key": mask_key(data["key"]), "note": data.get("note") or "",
            "expiresAt": data.get("expiresAt"), "deviceName": DEVICE_NAME,
            "message": "İnternet yoxdur — lisenziya son yoxlamaya əsasən açıqdır." if offline else "",
        }

    def _fail(self, code: str, message: str, clear: bool = False) -> Dict[str, Any]:
        if clear:
            self.state.clear()
        self.licensed = False
        return {"licensed": False, "admin": self.admin, "reason": code, "message": message, "deviceName": DEVICE_NAME}

    # --- onlayn təsdiq + lazım olsa bu kompüterə bağlama
    def _confirm(self, key: str) -> Dict[str, Any]:
        doc, now = fetch_license(key)
        _validate(doc, now)
        if not doc.get("deviceId"):
            values = {"deviceId": DEVICE_ID, "deviceName": DEVICE_NAME, "activatedAt": now, "lastSeen": now}
            status, payload, now = _patch(key, values, update_time=doc.get("_updateTime"))
            if status != 200:
                detail = _error_detail(payload, status)
                if detail in ("FAILED_PRECONDITION", "ABORTED") or status in (409, 412):
                    # eyni anda başqa kompüter aktivləşdirdi — yenidən yoxla
                    doc, now = fetch_license(key)
                    _validate(doc, now)
                    if doc.get("deviceId") != DEVICE_ID:
                        raise LicenseError("other_device", MESSAGES["other_device"])
                elif status == 403:
                    raise LicenseError("rules", MESSAGES["rules"])
                else:
                    raise LicenseError("server", MESSAGES["server"].format(detail=detail))
        else:
            try:
                _patch(key, {"lastSeen": now})       # "son görülmə" — uğursuz olsa da problem deyil
            except OfflineError:
                pass
        expires = doc.get("expiresAt")
        data = {
            "key": key, "deviceId": DEVICE_ID, "validatedAt": _iso(now),
            "expiresAt": _iso(expires) if isinstance(expires, datetime) else None,
            "note": doc.get("note") or "",
        }
        self.state.save(data)
        return self._ok(data)

    def _offline(self, data: Dict[str, Any]) -> Dict[str, Any]:
        now = _utcnow()
        validated = _parse_iso(data.get("validatedAt"))
        expires = _parse_iso(data.get("expiresAt"))
        in_grace = bool(validated) and validated - timedelta(hours=1) <= now <= validated + timedelta(days=OFFLINE_GRACE_DAYS)
        if in_grace and not (expires and expires <= now):
            return self._ok(data, offline=True)
        return self._fail("offline_expired", MESSAGES["offline_expired"])

    # --- API
    def status(self) -> Dict[str, Any]:
        with self.lock:
            data = self.state.load()
            if not data:
                return self._fail("no_key", "")
            try:
                return self._confirm(data["key"])
            except OfflineError:
                return self._offline(data)
            except LicenseError as exc:
                if exc.code in ("rules", "server", "ssl"):
                    return self._offline(data)          # serverdə problem — istifadəçini dərhal bağlamırıq
                return self._fail(exc.code, exc.message, clear=True)

    def activate(self, raw_key: str) -> Dict[str, Any]:
        with self.lock:
            key = normalize_key(raw_key)
            if not key:
                return self._fail("bad_format", MESSAGES["bad_format"])
            try:
                return self._confirm(key)
            except OfflineError:
                return self._fail("offline", MESSAGES["offline"])
            except LicenseError as exc:
                return self._fail(exc.code, exc.message)

    def admin_login(self, id_token: str) -> None:
        if not id_token:
            raise LicenseError("admin", "Admin sessiyası təsdiqlənmədi.")
        try:
            status, payload, _ = _http("POST", LOOKUP_URL, params={"key": FIREBASE_CONFIG["apiKey"]},
                                       body={"idToken": id_token})
        except OfflineError:
            raise LicenseError("offline", "İnternet bağlantısı yoxdur.")
        users = payload.get("users") or []
        email = str(users[0].get("email") or "").lower() if users else ""
        if status != 200 or not email:
            raise LicenseError("admin", "Admin sessiyası təsdiqlənmədi. Yenidən daxil ol.")
        if email != ADMIN_EMAIL.lower():
            raise LicenseError("not_admin", "Bu hesab admin deyil.")
        self.admin = True

    def admin_logout(self) -> None:
        self.admin = False


# ---------------------------------------------------------------- FastAPI
class KeyBody(BaseModel):
    key: str = ""


class TokenBody(BaseModel):
    idToken: str = ""


class MailBody(BaseModel):
    kind: str = "gmail"     # "gmail" → brauzerdə Gmail, "mail" → kompüterin poçt proqramı
    plan: str = ""


def sales_mail_url(kind: str, plan_id: str) -> str:
    """Qəbz göndərmək üçün hazır məktub linki (mövzu və mətn doldurulmuş)."""
    plans = SALES["plans"]
    plan = next((p for p in plans if p["id"] == plan_id), plans[0])
    price = f'{plan["price"]:.2f} {SALES["currency"]}'
    subject = f'Exam Viewer lisenziya — {plan["title"]} ({price})'
    body = (
        "Salam!\n\n"
        f'Exam Viewer üçün {plan["label"]} lisenziya almaq istəyirəm.\n'
        f'Tarif: {plan["title"]} — {price}\n'
        f"Kompüter: {DEVICE_NAME}\n\n"
        "Ödəniş qəbzi əlavədədir.\n"
    )
    quote = lambda text: urllib.parse.quote(text, safe="")
    if kind == "mail":
        return f'mailto:{SALES["email"]}?subject={quote(subject)}&body={quote(body)}'
    return (f'https://mail.google.com/mail/?view=cm&fs=1&to={quote(SALES["email"])}'
            f"&su={quote(subject)}&body={quote(body)}")


OPEN_PATHS = {"/", "/health", "/favicon.ico", "/app-config", "/license/status", "/license/activate",
              "/admin/session", "/admin/logout", "/sales/open-mail"}


def install(app, data_dir: Path) -> LicenseManager:
    """Lisenziya yoxlamasını FastAPI tətbiqinə qoşur. launcher.py çağırır."""
    from fastapi.responses import JSONResponse

    manager = LicenseManager(data_dir)

    # Yalnız oxuma (GET) sərbəstdir — şəkillər və sual mətni üçün. Yazma əməliyyatları
    # (lisenziya, admin, PDF yükləmə) yalnız proqramın öz pəncərəsindən gələ bilər.
    @app.middleware("http")
    async def require_session(request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        if request.cookies.get(SESSION_COOKIE) == SESSION_TOKEN:
            return await call_next(request)
        return JSONResponse({"detail": "İcazəsiz sorğu."}, status_code=403)

    @app.middleware("http")
    async def license_gate(request, call_next):
        if request.url.path in OPEN_PATHS or manager.allowed():
            return await call_next(request)
        return JSONResponse({"detail": "Lisenziya tələb olunur."}, status_code=403)

    @app.get("/app-config")
    def app_config():
        return {"firebase": FIREBASE_CONFIG, "adminEmail": ADMIN_EMAIL, "licenseCollection": LICENSE_COLLECTION,
                "sales": SALES}

    @app.post("/sales/open-mail")
    def sales_open_mail(body: MailBody):
        # yalnız hazır şablon açılır — ixtiyari link açmaq olmur
        try:
            opened = webbrowser.open(sales_mail_url(body.kind, body.plan))
        except Exception:
            opened = False
        return {"ok": bool(opened)}

    @app.get("/license/status")
    def license_status():
        return manager.status()

    @app.post("/license/activate")
    def license_activate(body: KeyBody):
        return manager.activate(body.key)

    @app.post("/admin/session")
    def admin_session(body: TokenBody):
        try:
            manager.admin_login(body.idToken)
        except LicenseError as exc:
            return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=403)
        return {"admin": True}

    @app.post("/admin/logout")
    def admin_logout():
        manager.admin_logout()
        return {"admin": False}

    return manager
