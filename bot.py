#!/usr/bin/env python3
"""
TrendBTC Multi-Account Auto-Bot
- Support unlimited accounts (1 refresh token per line di accounts.txt)
- Auto-refresh token per account (independent rotation)
- Auto-predict UP/DOWN setiap round (15 menit)
- Auto-open box, auto-claim faucet, auto-tasks
- Strategy: Follow the crowd (pilih pool yang lebih besar)
- Anti-detection: random delay antar akun
"""
import requests
import json
import time
import uuid
import os
import random
from datetime import datetime

API = "https://api.trendbtc.app"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(SCRIPT_DIR, "accounts.txt")
ACCOUNTS_BACKUP_FILE = os.path.join(SCRIPT_DIR, "accounts.txt.bak")

# Strategy config
STAKE_AMOUNT = 10
MIN_STAKE_AMOUNT = 1     # USD. Sesuai batas minimum stake di situs TrendBTC ($1–$saldo). Kalau saldo di bawah ini, skip predict round tersebut
STRATEGY_POOL = ["random", "minority", "crowd"]  # tiap akun pilih sendiri secara acak setiap round, jadi tiap akun bisa dapat pola berbeda-beda
DELAY_MIN = 5    # detik, delay minimum antar akun
DELAY_MAX = 15   # detik, delay maksimum antar akun

# Token persistence / refresh config
ACCESS_TOKEN_MAX_AGE = 600   # detik. Refresh proaktif setelah ini (anggap access token ~15-30 menit, kita ambil margin aman 10 menit)
REFRESH_RETRY_ATTEMPTS = 3   # jumlah percobaan ulang kalau refresh gagal karena error jaringan (bukan token invalid)
REFRESH_RETRY_DELAY = 5      # detik antar percobaan ulang

# Retry untuk request API biasa (predict, balance, box, dll) kalau timeout/koneksi bermasalah
REQUEST_TIMEOUT = 20         # detik, dinaikkan sedikit dari 15 karena koneksi mobile kadang lambat
REQUEST_RETRY_ATTEMPTS = 3
REQUEST_RETRY_DELAY = 3      # detik antar percobaan ulang

# Task "like" (onboarding) yang mau di-auto-claim, diidentifikasi lewat clickId masing-masing
ONBOARDING_LIKE_CLICK_IDS = [
    "f033f540-0043-4021-98ae-92fb9ab7e2f2",
    "970f3136-1eb3-4f57-9d33-574656261062",
    "a541c604-db5b-48b6-8370-b820814a841e",
]

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

class Account:
    """Satu akun TrendBTC dengan token management independen"""
    
    def __init__(self, idx, refresh_token):
        self.idx = idx
        self.refresh_token = refresh_token
        self.access_token = None
        self.email = None
        self.alive = True
        self.token_time = 0  # timestamp terakhir access_token diperoleh

    def save_refresh_token(self, all_tokens):
        """Update token di accounts.txt secara atomic (tulis ke file sementara lalu rename),
        supaya kalau proses mati di tengah jalan, accounts.txt lama tidak ikut rusak/kosong."""
        all_tokens[self.idx] = self.refresh_token
        tmp_path = ACCOUNTS_FILE + ".tmp"
        try:
            # Backup versi sebelum ditimpa, buat jaga-jaga/diagnosa
            if os.path.exists(ACCOUNTS_FILE):
                try:
                    with open(ACCOUNTS_FILE, "r") as src, open(ACCOUNTS_BACKUP_FILE, "w") as bak:
                        bak.write(src.read())
                except Exception:
                    pass
            with open(tmp_path, "w") as f:
                f.write("\n".join(all_tokens) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, ACCOUNTS_FILE)
        except Exception as e:
            log(f"  [Akun {self.idx+1}] ⚠️ Gagal menyimpan refresh token ke disk: {e}")

    def needs_refresh(self):
        """True kalau access token belum ada atau sudah mendekati kadaluarsa."""
        if not self.access_token:
            return True
        return (time.time() - self.token_time) > ACCESS_TOKEN_MAX_AGE

    def refresh_access(self, all_tokens):
        """Refresh access token. Retry beberapa kali kalau errornya jaringan/timeout
        (bukan token invalid), supaya akun tidak langsung dianggap mati permanen
        gara-gara gangguan koneksi sesaat."""
        last_network_error = None
        for attempt in range(1, REFRESH_RETRY_ATTEMPTS + 1):
            try:
                r = requests.post(f"{API}/api/auth/refresh",
                    headers={"Content-Type": "application/json"},
                    json={"refreshToken": self.refresh_token},
                    timeout=15)

                if r.status_code == 200:
                    data = r.json()
                    self.access_token = data["accessToken"]
                    self.token_time = time.time()
                    new_refresh = data.get("refreshToken")
                    if new_refresh and new_refresh != self.refresh_token:
                        self.refresh_token = new_refresh
                        self.save_refresh_token(all_tokens)
                    self.alive = True
                    return True
                elif r.status_code in (401, 400, 403):
                    # Refresh token benar-benar ditolak server -> tidak ada gunanya diulang
                    log(f"  [Akun {self.idx+1}] ❌ Refresh token invalid/revoked: {r.text[:100]}")
                    self.alive = False
                    return False
                else:
                    # Kemungkinan error server sementara, boleh dicoba lagi
                    last_network_error = f"HTTP {r.status_code}: {r.text[:100]}"
            except Exception as e:
                last_network_error = str(e)

            if attempt < REFRESH_RETRY_ATTEMPTS:
                time.sleep(REFRESH_RETRY_DELAY)

        log(f"  [Akun {self.idx+1}] ❌ Refresh gagal setelah {REFRESH_RETRY_ATTEMPTS}x percobaan: {last_network_error}")
        # Gagal karena jaringan/server, bukan karena token ditolak -> jangan matikan akun,
        # supaya masih dicoba lagi di siklus berikutnya dengan refresh_token yang sama.
        return False
    
    def _request(self, method, path, headers, json_body=None):
        """Kirim request dengan retry otomatis kalau kena timeout/connection error.
        Error dari server (4xx/5xx dengan response) TIDAK diretry di sini, cuma
        kegagalan koneksi/timeout yang biasanya cuma gangguan jaringan sesaat."""
        last_error = None
        for attempt in range(1, REQUEST_RETRY_ATTEMPTS + 1):
            try:
                if method == "GET":
                    return requests.get(f"{API}{path}", headers=headers, timeout=REQUEST_TIMEOUT)
                else:
                    return requests.post(f"{API}{path}", headers=headers, json=json_body, timeout=REQUEST_TIMEOUT)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_error = e
                if attempt < REQUEST_RETRY_ATTEMPTS:
                    time.sleep(REQUEST_RETRY_DELAY)
        raise last_error

    def api_get(self, path, auth=True, all_tokens=None):
        if auth and all_tokens is not None and self.needs_refresh():
            self.refresh_access(all_tokens)
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            r = self._request("GET", path, headers)
        except Exception as e:
            return {"error": f"Koneksi gagal: {e}"}
        if r.status_code == 401 and all_tokens:
            if self.refresh_access(all_tokens):
                headers["Authorization"] = f"Bearer {self.access_token}"
                try:
                    r = self._request("GET", path, headers)
                except Exception as e:
                    return {"error": f"Koneksi gagal: {e}"}
        try:
            data = r.json()
        except:
            data = {"error": f"HTTP {r.status_code} (server error, respons bukan JSON — kemungkinan server sedang down)"}
        if r.status_code in (200, 201):
            return data
        if "error" not in data:
            data = {"error": data}
        return data
    
    def api_post(self, path, body, auth=True, all_tokens=None):
        if auth and all_tokens is not None and self.needs_refresh():
            self.refresh_access(all_tokens)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            r = self._request("POST", path, headers, json_body=body)
        except Exception as e:
            return {"error": f"Koneksi gagal: {e}"}
        if r.status_code == 401 and all_tokens:
            if self.refresh_access(all_tokens):
                headers["Authorization"] = f"Bearer {self.access_token}"
                try:
                    r = self._request("POST", path, headers, json_body=body)
                except Exception as e:
                    return {"error": f"Koneksi gagal: {e}"}
        try:
            data = r.json()
        except:
            data = {"error": f"HTTP {r.status_code} (server error, respons bukan JSON — kemungkinan server sedang down)"}
        if r.status_code in (200, 201):
            return data
        if "error" not in data:
            data = {"error": data}
        return data
    
    def get_profile(self, all_tokens):
        data = self.api_get("/api/me", all_tokens=all_tokens)
        if "user" in data:
            self.email = data["user"].get("email", f"akun{self.idx+1}")
            return data["user"]
        return None
    
    def get_balance(self, all_tokens):
        data = self.api_get("/api/me/balances", all_tokens=all_tokens)
        balances = {}
        if "balances" in data:
            for b in data["balances"]:
                balances[b["currency"]] = b["balance"]
        return balances
    
    def get_my_prediction(self, all_tokens):
        data = self.api_get("/api/markets/btc/current-round/my-prediction", all_tokens=all_tokens)
        return data.get("prediction")
    
    def predict(self, round_id, direction, stake, all_tokens):
        idempotency = str(uuid.uuid4())
        body = {
            "roundId": round_id,
            "direction": direction,
            "stakeAmount": stake,
            "idempotencyKey": idempotency
        }
        result = self.api_post("/api/predictions", body, all_tokens=all_tokens)
        
        if "prediction" in result:
            pred = result["prediction"]
            log(f"  [Akun {self.idx+1}] ✅ Predicted {direction} | {stake} USD | {pred.get('status')}")
            return pred
        if "error" in result:
            err = result["error"]
            if isinstance(err, dict):
                code = err.get("code", "")
                msg = err.get("message", str(err))
                if code == "PREDICTION_ALREADY_EXISTS":
                    log(f"  [Akun {self.idx+1}] ⚠️ Already predicted this round")
                else:
                    log(f"  [Akun {self.idx+1}] ❌ Predict: {msg[:100]}")
            return None
        return result
    
    def _box_reward_type(self, result):
        """Ambil rewardType dari hasil buka box, tahan banting kalau strukturnya beda-beda."""
        opening = result.get("opening", result) if isinstance(result, dict) else {}
        return str(opening.get("rewardType", "")).upper()

    def _open_box_with_ticket(self, all_tokens, max_chain=5):
        """Buka box pakai tiket yang sudah dimiliki. Kalau reward yang keluar berupa tiket lagi,
        langsung dipakai lagi (chaining) sampai max_chain kali atau sampai kehabisan/gagal."""
        chain_attempts = 0
        last_result = None
        while chain_attempts < max_chain:
            chain_attempts += 1
            ticket_result = self.api_post("/api/boxes/trend_box_standard/open", {"useTicket": True}, all_tokens=all_tokens)
            if "error" in ticket_result:
                err = ticket_result["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                log(f"  [Akun {self.idx+1}] 🎟️ Pakai tiket gagal: {msg[:100]}")
                break
            log(f"  [Akun {self.idx+1}] 🎟️ Box dibuka pakai tiket! {json.dumps(ticket_result)[:150]}")
            last_result = ticket_result
            reward_type = self._box_reward_type(ticket_result)
            if "TICKET" not in reward_type:
                break
            time.sleep(1)
        return last_result

    def open_box(self, all_tokens):
        result = self.api_post("/api/boxes/trend_box_standard/open", {"useTicket": False}, all_tokens=all_tokens)
        if "error" in result:
            err = result["error"]
            if isinstance(err, dict):
                msg = err.get("message", str(err))
                if "cooldown" in msg.lower() or "available again" in msg.lower():
                    pass  # Skip logging cooldown setiap cycle
                else:
                    log(f"  [Akun {self.idx+1}] 📦 Box: {msg[:100]}")
            return None
        log(f"  [Akun {self.idx+1}] 📦 Box opened! {json.dumps(result)[:150]}")

        # Kalau reward-nya berupa TIKET, langsung dipakai buat buka box tambahan (tanpa nunggu cooldown)
        reward_type = self._box_reward_type(result)
        if "TICKET" in reward_type:
            time.sleep(1)
            self._open_box_with_ticket(all_tokens)

        return result

    def claim_onboarding_ticket(self, all_tokens):
        """Klaim tiket dari endpoint onboarding, lalu langsung pakai buat buka box."""
        result = self.api_post("/api/me/onboarding/claim", {}, all_tokens=all_tokens)
        if "error" in result:
            err = result["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            log(f"  [Akun {self.idx+1}] 🎫 Onboarding ticket: {msg[:150]}")
            return None

        log(f"  [Akun {self.idx+1}] 🎫 Onboarding ticket diklaim! {json.dumps(result)[:150]}")
        time.sleep(1)
        self._open_box_with_ticket(all_tokens)
        return result

    def claim_onboarding_like_tasks(self, all_tokens):
        """Auto-claim task 'like' onboarding satu-satu berdasarkan clickId di ONBOARDING_LIKE_CLICK_IDS."""
        for click_id in ONBOARDING_LIKE_CLICK_IDS:
            result = self.api_post("/api/me/onboarding/claim", {"clickId": click_id}, all_tokens=all_tokens)
            if "error" in result:
                err = result["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                log(f"  [Akun {self.idx+1}] 👍 Like task ({click_id[:8]}...): {msg[:150]}")
            else:
                log(f"  [Akun {self.idx+1}] 👍 Like task ({click_id[:8]}...) diklaim! {json.dumps(result)[:120]}")
            time.sleep(1)
    
    def claim_faucet(self, all_tokens):
        status = self.api_get("/api/me/faucet", all_tokens=all_tokens)
        if "error" in status:
            err = status["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            log(f"  [Akun {self.idx+1}] 🚰 Faucet status error: {msg[:100]}")
            return None
        if status.get("eligible", False):
            result = self.api_post("/api/me/faucet/claim", {}, all_tokens=all_tokens)
            if "error" not in result:
                log(f"  [Akun {self.idx+1}] 🚰 Faucet claimed! {json.dumps(result)[:150]}")
                return result
            else:
                err = result["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                log(f"  [Akun {self.idx+1}] 🚰 Faucet claim gagal: {msg[:100]}")
                return None
        else:
            next_claim = status.get("nextClaimAt") or status.get("availableAt")
            if next_claim:
                log(f"  [Akun {self.idx+1}] 🚰 Faucet belum eligible (cooldown, tersedia lagi: {next_claim})")
            # kalau tidak ada info cooldown, diamkan biar tidak spam log tiap cycle
            return None
    
    def check_tasks(self, all_tokens):
        data = self.api_get("/api/tasks", auth=False)
        tasks = data.get("tasks", [])
        my_tasks = self.api_get("/api/me/tasks?limit=50", all_tokens=all_tokens)
        completed = set()
        if "completions" in my_tasks:
            for c in my_tasks["completions"]:
                if c.get("status") in ("completed", "claimed"):
                    completed.add(c.get("taskId"))
        
        for t in tasks:
            if t.get("status") == "published" and t["id"] not in completed:
                result = self.api_post(f"/api/tasks/{t['id']}/claim", {}, all_tokens=all_tokens)
                if "error" not in result:
                    log(f"  [Akun {self.idx+1}] 📋 Task claimed: {t['title'][:40]}")
    
    def run_cycle(self, round_data, all_tokens):
        """Run 1 cycle untuk akun ini"""
        round_id = round_data["id"]
        status = round_data["status"]
        
        # Profile (sekali saja)
        if not self.email:
            self.get_profile(all_tokens)
        
        tag = f"[Akun {self.idx+1}]{f' {self.email}' if self.email else ''}"
        
        if status == "open":
            my_pred = self.get_my_prediction(all_tokens)
            balances = self.get_balance(all_tokens)
            bal_str = f"💰 TREND: {balances.get('TREND',0)} | USD: {balances.get('USD_BALANCE',0)} | SOL: {balances.get('SOL',0)}" if balances else ""
            if my_pred:
                log(f"  {tag} ✅ Already predicted: {my_pred['direction']} {my_pred['stakeAmount']} | {bal_str}")
            else:
                direction, strategy = self.decide_direction(round_data)
                stake = self.decide_stake(balances)
                if stake is None:
                    log(f"  {tag} ⏭️ Skip predict, saldo tidak cukup (min {MIN_STAKE_AMOUNT} USD) | {bal_str}")
                else:
                    if stake < STAKE_AMOUNT:
                        log(f"  {tag} 🎯 [{strategy}] Predicting: {direction} ({stake} USD, diturunkan dari {STAKE_AMOUNT} karena saldo terbatas) | {bal_str}")
                    else:
                        log(f"  {tag} 🎯 [{strategy}] Predicting: {direction} ({stake} USD) | {bal_str}")
                    self.predict(round_id, direction, stake, all_tokens)
        
        # Box
        self.open_box(all_tokens)
        
        # Onboarding ticket (klaim tiket lalu langsung dipakai buka box)
        self.claim_onboarding_ticket(all_tokens)
        
        # Onboarding "like" tasks (clickId spesifik)
        self.claim_onboarding_like_tasks(all_tokens)
        
        # Faucet
        self.claim_faucet(all_tokens)
        
        # Tasks
        self.check_tasks(all_tokens)
        
        # Balance (if not already shown in predict line)
        if status != "open":
            balances = self.get_balance(all_tokens)
            if balances:
                log(f"  {tag} 💰 TREND: {balances.get('TREND',0)} | USD: {balances.get('USD_BALANCE',0)} | SOL: {balances.get('SOL',0)}")
    
    def decide_stake(self, balances):
        """Tentukan besar stake berdasarkan saldo USD yang tersedia.
        - Kalau saldo >= STAKE_AMOUNT, pakai STAKE_AMOUNT seperti biasa.
        - Kalau saldo kurang tapi masih >= MIN_STAKE_AMOUNT, turunkan stake ke saldo yang ada.
        - Kalau saldo di bawah MIN_STAKE_AMOUNT, return None (skip predict round ini).
        """
        try:
            available = float(balances.get("USD_BALANCE", 0)) if balances else 0
        except (TypeError, ValueError):
            available = 0

        if available >= STAKE_AMOUNT:
            return STAKE_AMOUNT
        if available >= MIN_STAKE_AMOUNT:
            # Bulatkan ke bawah 2 desimal biar tidak kena masalah floating point saat dikirim ke API
            return int(available * 100) / 100
        return None

    def decide_direction(self, round_data):
        up_pool = round_data.get("upPool", 0)
        down_pool = round_data.get("downPool", 0)
        strategy = random.choice(STRATEGY_POOL)  # tiap akun, tiap round, pilih strategi sendiri-sendiri
        
        if strategy == "minority":
            # Ikut pool yang lebih kecil (contrarian)
            if up_pool < down_pool:
                direction = "UP"
            elif down_pool < up_pool:
                direction = "DOWN"
            else:
                direction = random.choice(["UP", "DOWN"])
        elif strategy == "crowd":
            if up_pool > down_pool:
                direction = "UP"
            elif down_pool > up_pool:
                direction = "DOWN"
            else:
                direction = random.choice(["UP", "DOWN"])
        else:  # "random"
            direction = random.choice(["UP", "DOWN"])
        
        return direction, strategy


def load_accounts():
    """Load refresh tokens dari accounts.txt"""
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    with open(ACCOUNTS_FILE, "r") as f:
        tokens = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    return tokens


def get_current_round():
    """Fetch current round (public, no auth)"""
    try:
        r = requests.get(f"{API}/api/markets/btc/current-round", timeout=15)
        if r.status_code == 200:
            return r.json().get("round", {})
    except:
        pass
    return {}


def run_forever():
    log("🚀 TrendBTC Multi-Account Bot Started")
    log(f"   Strategy: tiap akun pilih acak sendiri dari {STRATEGY_POOL} setiap round")
    log(f"   Stake: {STAKE_AMOUNT} USD per round")
    
    # Load accounts
    log(f"   📂 Membaca token dari: {ACCOUNTS_FILE}")
    token_list = load_accounts()
    if not token_list:
        log(f"❌ No accounts in {ACCOUNTS_FILE}! Put 1 refresh token per line.")
        return

    accounts = [Account(i, t) for i, t in enumerate(token_list)]
    log(f"   Accounts loaded: {len(accounts)}")
    for a in accounts:
        preview = f"{a.refresh_token[:6]}...{a.refresh_token[-4:]}" if len(a.refresh_token) > 12 else "(pendek)"
        log(f"   [Akun {a.idx+1}] token yang terbaca: {preview}")
    
    # Initial refresh semua akun
    all_tokens = [a.refresh_token for a in accounts]
    for acc in accounts:
        if not acc.refresh_access(all_tokens):
            if acc.alive:
                log(f"  [Akun {acc.idx+1}] ⏳ Refresh gagal sementara (jaringan), akan dicoba lagi tiap round")
            else:
                log(f"  [Akun {acc.idx+1}] 💀 Refresh token invalid — perlu masukkan token baru di {ACCOUNTS_FILE}")
        else:
            log(f"  [Akun {acc.idx+1}] ✅ Token OK")
        time.sleep(1)
    
    alive_accounts = [a for a in accounts if a.alive]
    if not alive_accounts:
        log("❌ No alive accounts!")
        return
    
    log(f"   Alive accounts: {len(alive_accounts)}/{len(accounts)}")
    
    last_round_id = None
    
    while True:
        try:
            round_data = get_current_round()
            if not round_data:
                time.sleep(30)
                continue
            
            current_round_id = round_data["id"]
            
            if current_round_id != last_round_id:
                # Round baru — run cycle untuk semua akun
                status = round_data.get("status", "")
                price = round_data.get("startPrice", 0)
                up = round_data.get("upPool", 0)
                down = round_data.get("downPool", 0)
                
                # === Dynamic account loading ===
                token_list = load_accounts()
                # Cek ada akun baru
                while len(accounts) < len(token_list):
                    new_idx = len(accounts)
                    new_acc = Account(new_idx, token_list[new_idx])
                    accounts.append(new_acc)
                    log(f"  🆕 Akun {new_idx+1} added!")
                # Update token untuk akun yang berubah manual
                for i, acc in enumerate(accounts):
                    if i < len(token_list) and acc.refresh_token != token_list[i]:
                        acc.refresh_token = token_list[i]
                        acc.alive = True
                all_tokens = list(token_list)
                
                log("=" * 55)
                log(f"🟢 NEW ROUND | {status} | Price: ${price} | UP: {up} | DOWN: {down}")
                log(f"   Accounts: {len([a for a in accounts if a.alive])}/{len(accounts)} alive")
                log("=" * 55)
                
                for acc in accounts:
                    if not acc.alive:
                        continue
                    try:
                        acc.run_cycle(round_data, all_tokens)
                    except Exception as e:
                        log(f"  [Akun {acc.idx+1}] ❌ Error: {e}")
                    
                    # Delay antar akun (anti-detection)
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    time.sleep(delay)
                
                log("=" * 55)
                log(f"✅ All accounts done. Next round in ~15 min.")
                log("=" * 55)
                
                # Total balance summary like Gram
                total_trend = 0
                total_usd = 0
                total_sol = 0
                for acc in accounts:
                    if not acc.alive:
                        continue
                    b = acc.get_balance(all_tokens)
                    if b:
                        total_trend += float(b.get('TREND', 0))
                        total_usd += float(b.get('USD_BALANCE', 0))
                        total_sol += float(b.get('SOL', 0))
                log(f"📊 TOTAL BALANCE: TREND: {total_trend} | USD: {total_usd} | SOL: {total_sol}")
                alive = len([a for a in accounts if a.alive])
                log(f"   AVERAGE: TREND: {total_trend/alive:.1f} | USD: {total_usd/alive:.1f}")
                log("=" * 55)
                
                last_round_id = current_round_id
            else:
                # Same round, check settled
                status = round_data.get("status")
                if status == "settled":
                    result = round_data.get("result")
                    log(f"🏁 Round settled! Result: {result}")
            
            # Sleep 60s
            time.sleep(60)
            
        except KeyboardInterrupt:
            log("Stopped by user")
            break
        except Exception as e:
            log(f"❌ Main error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    run_forever()
