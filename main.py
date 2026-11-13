import html as htmllib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

import webview
from openai import OpenAI

CONFIG_FILE = "agent_config.json"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Модели по умолчанию. У каждой свой API-ключ и адрес провайдера —
# всё настраивается в приложении (кнопка ⚙ Модели).
DEFAULT_MODELS = [
    "gemini-3.8-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]

# Только для Gemini: "none" — самый быстрый, "low" — баланс, "medium" — точнее
REASONING = "low"

MAX_STEPS = 10
MAX_ACTIONS = 5
CMD_TIMEOUT = 25
CMD_TIMEOUT_NET = 45
MAX_OUTPUT = 3000
MODEL_COOLDOWN = 60
FETCH_LIMIT = 400_000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

BASE_PROMPT = """Ты — автономный ИИ-агент с доступом к Windows через командную строку (CMD).
Выполняй задачи пользователя быстро и точно.

Формат ответа:
МЫСЛЬ: одна короткая строка на русском.
EXECUTE: команда_для_CMD

Правила:
- Если несколько команд идут строго по порядку и не зависят от вывода друг друга, пиши их подряд, каждую с новой строки с префиксом EXECUTE: (максимум 5). Они выполнятся по очереди, а все результаты придут тебе сразу. Это экономит шаги.
- Если следующая команда зависит от результата предыдущей, пиши только одну.
- Не придумывай ответ за систему, дождись результата.
- Не проверяй результат лишними командами, если ошибки не было.
- Синтаксис Windows CMD. Для записи текста в файл: powershell -Command "Set-Content -Path 'файл.txt' -Value 'текст' -Encoding UTF8"
- Когда задача полностью выполнена, ответь без EXECUTE и коротко сообщи итог.

Пример:
Пользователь: Создай текстовый файл на рабочем столе.
МЫСЛЬ: Создаю пустой файл note.txt на рабочем столе.
EXECUTE: echo. > "%USERPROFILE%\\Desktop\\note.txt\""""

NETWORK_PROMPT = """

Сетевые инструменты ВКЛЮЧЕНЫ. Тебе доступно:
- FETCH: https://адрес — быстро получить текст веб-страницы или ответ API (GET). Предпочитай это вместо curl, если нужно просто прочитать страницу.
- CMD-команды: ping, tracert, nslookup, netstat, ipconfig, curl.exe, Test-NetConnection (через powershell), скачивание файлов через curl.exe -L -o файл URL.
Никогда не запускай скачанный код автоматически."""

NETWORK_OFF_PROMPT = "\n\nСетевые инструменты ОТКЛЮЧЕНЫ: не используй команды, которым нужен интернет или сеть."

DANGEROUS = re.compile(
    r"(\bformat\s+[a-z]:|\bformat-volume\b|\bdiskpart\b|\bbcdedit\b|\bcipher\s+/w\b"
    r"|\b(rmdir|rd|del|erase)\b.*\b[a-z]:\\?\s*$"
    r"|\b(rmdir|rd)\b.*\s/s\b.*\b[a-z]:\\(windows|users|program)"
    r"|\breg\s+delete\b|\bshutdown\b"
    r"|\biex\b|invoke-expression|-encodedcommand|\s-enc\s)",
    re.IGNORECASE,
)
NETWORK_RE = re.compile(
    r"\b(ping|tracert|pathping|nslookup|netstat|curl|wget|ssh|scp|sftp|ftp|telnet|netsh"
    r"|nbtstat|bitsadmin|invoke-webrequest|invoke-restmethod|iwr|irm|test-netconnection"
    r"|start-bitstransfer|webclient|downloadstring|downloadfile)\b|net\s+use\b|certutil.*urlcache",
    re.IGNORECASE,
)
ACTION_RE = re.compile(r"(EXECUTE|FETCH):[ \t]*([^\n]+)")
TEMP_CODES = {408, 429, 500, 502, 503, 504}
TEMP_HINTS = ("timed out", "timeout", "overloaded", "unavailable", "connection", "empty", "high demand")


class Cancelled(Exception):
    pass


def base_path():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def html_path():
    root = sys._MEIPASS if getattr(sys, "frozen", False) else base_path()
    path = os.path.join(root, "index.html")
    if not os.path.exists(path):
        print(f"Внимание: файл {path} не найден!")
    return path


def fetch_url(url):
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return "Ошибка: FETCH работает только с http/https."
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 PC-Agent"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read(FETCH_LIMIT)
            charset = resp.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
        if "html" in ctype.lower() or text.lstrip().lower().startswith(("<!doctype", "<html")):
            text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = htmllib.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
        return text or "Пустой ответ."
    except Exception as e:
        return f"Ошибка запроса: {e}"


def run_cancellable(func, stop, poll=0.1):
    """Выполняет func в отдельном потоке; при stop сразу выбрасывает Cancelled."""
    box = {}

    def work():
        try:
            box["result"] = func()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    while t.is_alive():
        if stop.is_set():
            raise Cancelled()
        t.join(poll)
    if "error" in box:
        raise box["error"]
    return box["result"]


class ApiBridge:
    def __init__(self):
        self._window = None
        self.config_path = os.path.join(base_path(), CONFIG_FILE)
        self._stop = threading.Event()
        self._run_id = 0
        self._clients = {}
        self._cooldown = {}
        self._no_reasoning = set()
        self._last_model = None
        self._confirm_evt = threading.Event()
        self._confirm_ok = False

    # ---------- служебное ----------
    def _js(self, fn, *args):
        try:
            if self._window:
                params = ", ".join(json.dumps(a) for a in args)
                self._window.evaluate_js(f"{fn}({params})")
        except Exception:
            pass

    def _log(self, text, kind):
        self._js("logFromPython", text, kind)

    def _get_client(self, model):
        key = (model["base_url"], model["api_key"])
        if key not in self._clients:
            self._clients[key] = OpenAI(
                base_url=model["base_url"], api_key=model["api_key"], timeout=40, max_retries=0
            )
        return self._clients[key]

    # ---------- подтверждение (в самом окне, можно остановить) ----------
    def answer_confirm(self, ok):
        self._confirm_ok = bool(ok)
        self._confirm_evt.set()

    def _confirm(self, text, stop):
        self._confirm_evt.clear()
        self._confirm_ok = False
        self._js("askConfirm", text)
        while not self._confirm_evt.wait(0.1):
            if stop.is_set():
                self._js("closeConfirm")
                return False
        return self._confirm_ok

    # ---------- конфиг (доступен из JS) ----------
    def _load(self):
        data = {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            pass

        legacy_key = data.get("api_key") or data.get("openrouter_api_key", "")
        models = []
        raw = data.get("models") or []
        for m in raw:
            m = m if isinstance(m, dict) else {"name": str(m)}
            name = str(m.get("name", "")).strip()
            if not name or name in [x["name"] for x in models]:
                continue
            base_url = (m.get("base_url") or GEMINI_URL).strip()
            api_key = (m.get("api_key") or "").strip()
            if not api_key and base_url == GEMINI_URL:
                api_key = legacy_key  # миграция со старой версии
            models.append({"name": name, "enabled": bool(m.get("enabled", True)),
                           "base_url": base_url, "api_key": api_key})
        if not models:
            models = [{"name": n, "enabled": True, "base_url": GEMINI_URL, "api_key": legacy_key}
                      for n in DEFAULT_MODELS]

        names = [m["name"] for m in models]
        selected = data.get("selected")
        if selected not in names:
            selected = names[0]
        return {
            "models": models,
            "selected": selected,
            "network": bool(data.get("network", False)),
            "confirm": bool(data.get("confirm", True)),
        }

    def get_config(self):
        return self._load()

    def save_config(self, cfg):
        try:
            old = self._load()
            merged = {**old, **{k: v for k, v in cfg.items() if k in old}}
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=4, ensure_ascii=False)
            return True
        except (OSError, AttributeError):
            return False

    # ---------- запуск / остановка ----------
    def stop_agent(self):
        self._stop.set()

    def start_agent_task(self, user_task):
        self._stop.set()                 # на всякий случай гасим предыдущий запуск
        self._stop = threading.Event()   # у каждого запуска свой флаг остановки
        self._run_id += 1
        threading.Thread(
            target=self._run_agent_loop,
            args=(self._run_id, self._stop, user_task),
            daemon=True,
        ).start()

    # ---------- выполнение команд ----------
    def _run_cmd(self, command, timeout, stop):
        try:
            proc = subprocess.Popen(
                f"chcp 65001>nul & {command}",
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=NO_WINDOW,
            )
        except Exception as e:
            return f"Ошибка при выполнении команды: {e}"

        deadline = time.time() + timeout
        output, reason = "", None
        while True:
            try:
                output, _ = proc.communicate(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                if stop.is_set():
                    reason = "Команда прервана пользователем."
                elif time.time() > deadline:
                    reason = f"Ошибка: превышено время ожидания команды ({timeout} сек)."
                if reason:
                    self._kill_tree(proc)
                    try:
                        output, _ = proc.communicate(timeout=2)
                    except Exception:
                        output = ""
                    break

        output = (output or "").strip()
        if reason:
            return reason if not output else f"{reason}\n{self._trim(output)}"
        return self._trim(output) if output else "Выполнено успешно (вывод пуст)."

    @staticmethod
    def _kill_tree(proc):
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW, timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    @staticmethod
    def _trim(text):
        if len(text) > MAX_OUTPUT:
            return text[:MAX_OUTPUT] + "\n...[вывод обрезан]"
        return text

    @staticmethod
    def _check(kind, arg, network):
        if kind == "FETCH":
            return None if network else "Ошибка: сетевые инструменты отключены пользователем."
        if DANGEROUS.search(arg):
            return "Ошибка: команда заблокирована системой безопасности агента."
        if not network and NETWORK_RE.search(arg):
            return "Ошибка: сетевые инструменты отключены пользователем."
        return None

    # ---------- общение с ИИ ----------
    def _model_order(self, cfg):
        by_name = {m["name"]: m for m in cfg["models"]}
        order = [by_name[cfg["selected"]]] + [
            m for m in cfg["models"] if m["enabled"] and m["name"] != cfg["selected"]
        ]
        order = [m for m in order if m["api_key"]]
        now = time.time()
        rested = [m for m in order if self._cooldown.get(m["name"], 0) <= now]
        return rested or order

    def _try_model(self, model, messages, stop):
        """Возвращает (статус, текст). Статус: ok / skip / fatal."""
        name = model["name"]
        client = self._get_client(model)
        use_reasoning = "generativelanguage" in model["base_url"] and name not in self._no_reasoning

        for attempt in (1, 2):
            if stop.is_set():
                return "fatal", None
            kwargs = {"model": name, "messages": messages, "temperature": 0.1}
            if use_reasoning:
                kwargs["reasoning_effort"] = REASONING
            try:
                response = run_cancellable(lambda: client.chat.completions.create(**kwargs), stop)
                text = (response.choices[0].message.content or "").strip()
                if not text:
                    raise ValueError("empty response")
                return "ok", text
            except Cancelled:
                return "fatal", None
            except Exception as e:
                err = str(e)
                code = getattr(e, "status_code", None)
                low = err.lower()

                if code in (401, 403):
                    self._cooldown[name] = time.time() + 600
                    self._log(f"⚠️ {name}: ключ недействителен или нет доступа. Пробую другую модель...", "sys")
                    return "skip", None
                if code == 400 and "reasoning" in low and use_reasoning:
                    self._no_reasoning.add(name)
                    use_reasoning = False
                    continue
                if code in TEMP_CODES or any(h in low for h in TEMP_HINTS):
                    if attempt == 1:
                        if stop.wait(2):
                            return "fatal", None
                        continue
                    self._cooldown[name] = time.time() + MODEL_COOLDOWN
                    self._log(f"⏳ {name} перегружена, пробую другую...", "sys")
                    return "skip", None
                self._cooldown[name] = time.time() + 600
                self._log(f"⚠️ {name} недоступна ({err[:120]}), пробую другую...", "sys")
                return "skip", None
        return "skip", None

    def _ask_ai(self, cfg, messages, stop):
        started = time.time()
        for round_no in (1, 2):
            order = self._model_order(cfg)
            if not order:
                self._log("❌ Не указан API-ключ ни для одной модели (кнопка ⚙ Модели).", "ai")
                return None
            for model in order:
                status, text = self._try_model(model, messages, stop)
                if status == "ok":
                    if model["name"] != self._last_model:
                        self._last_model = model["name"]
                        self._js("setActiveModel", model["name"])
                    self._log(f"⏱ {model['name']} · {time.time() - started:.1f} сек", "sys")
                    return text
                if status == "fatal":
                    return None
            if round_no == 1:
                self._log("⏳ Все модели заняты, повтор через 5 сек...", "sys")
                if stop.wait(5):
                    return None
        self._log("❌ Ни одна модель не ответила. Проверьте список моделей, ключи и интернет.", "ai")
        return None

    # ---------- главный цикл ----------
    def _run_agent_loop(self, run_id, stop, user_task):
        try:
            self._agent_loop(stop, user_task)
        except Cancelled:
            pass
        except Exception as e:
            self._log(f"❌ Внутренняя ошибка: {e}", "ai")
        finally:
            if stop.is_set():
                self._log("⏹ Остановлено пользователем.", "ai")
            if run_id == self._run_id:
                self._js("taskFinished")

    def _agent_loop(self, stop, user_task):
        cfg = self._load()
        network, ask_confirm = cfg["network"], cfg["confirm"]
        cmd_timeout = CMD_TIMEOUT_NET if network else CMD_TIMEOUT
        prompt = BASE_PROMPT + (NETWORK_PROMPT if network else NETWORK_OFF_PROMPT)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_task},
        ]

        for step in range(1, MAX_STEPS + 1):
            if stop.is_set():
                return
            self._log(f"🤖 [Шаг {step}] Запрос к ИИ...", "ai")

            ai_text = self._ask_ai(cfg, messages, stop)
            if ai_text is None or stop.is_set():
                return

            self._log(ai_text, "ai")
            messages.append({"role": "assistant", "content": ai_text})

            actions = [(k, a.strip().strip("`").strip()) for k, a in ACTION_RE.findall(ai_text)][:MAX_ACTIONS]
            if not actions:
                self._log("✅ Задача выполнена агентом!", "ai")
                return

            errors = [self._check(k, a, network) for k, a in actions]
            runnable = [f"{k}: {a}" for (k, a), err in zip(actions, errors) if err is None]

            approved = True
            if ask_confirm and runnable:
                approved = self._confirm("\n".join(runnable), stop)
                if stop.is_set():
                    return

            results = []
            for (kind, arg), err in zip(actions, errors):
                if stop.is_set():
                    return
                if err:
                    out = err
                elif not approved:
                    out = "Пользователь отклонил выполнение команды."
                elif kind == "FETCH":
                    self._log(f"🌐 FETCH: {arg}", "cmd")
                    try:
                        out = self._trim(run_cancellable(lambda: fetch_url(arg), stop))
                    except Cancelled:
                        return
                else:
                    self._log(f"⌨️ Запуск CMD: {arg}", "cmd")
                    out = self._run_cmd(arg, cmd_timeout, stop)
                results.append(out)

            if stop.is_set():
                return

            if len(actions) == 1:
                cmd_out = results[0]
            else:
                cmd_out = "\n\n".join(
                    f"[{i}] {k}: {a}\n{r}" for i, ((k, a), r) in enumerate(zip(actions, results), 1)
                )

            self._log(f"Результат системы:\n{cmd_out}", "sys")
            messages.append({"role": "user", "content": f"Ответ системы:\n{cmd_out}"})

        self._log(f"⚠️ Достигнут лимит в {MAX_STEPS} шагов. Работа остановлена.", "ai")


if __name__ == "__main__":
    api = ApiBridge()
    api._window = webview.create_window(
        title="ИИ-Агент Управления ПК",
        url=html_path(),
        js_api=api,
        width=820,
        height=720,
    )
    webview.start()