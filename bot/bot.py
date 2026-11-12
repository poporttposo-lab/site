"""
Бот «Партнёры ИЖТ» — помощник студенческого совета ИЖТ в поиске партнёров.

Запуск:
    pip install -r requirements.txt
    set BOT_TOKEN=токен_от_BotFather        (Windows)
    set ADMIN_IDS=123456789,987654321       (Telegram ID членов совета)
    set AI_API_KEY=ключ_smertapi.top         (для подбора партнёров через ИИ)
    set AI_MODEL=kimi-k3                     (необязательно; список моделей — команда /models)
    python bot.py

Linux/Mac: export вместо set.
Свой Telegram ID можно узнать у бота @userinfobot.
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PROXY_URL = os.getenv("PROXY_URL")  # например socks5://127.0.0.1:1080 или http://127.0.0.1:8080
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "partners.db")  # на хостинге: /data/partners.db, чтобы база не стиралась при обновлении
REMIND_DAYS = 3  # через сколько дней напомнить о повторном контакте

SUPPORT = {
    "prize": "🎁 Призы",
    "venue": "🏢 Площадка",
    "expert": "🎤 Эксперт / спикер",
    "intern": "💼 Стажировки",
    "money": "💰 Финансы",
}
STATUS = {
    "new": "⚪ Новая",
    "written": "📨 Написали",
    "talks": "💬 Переговоры",
    "agreed": "✅ Согласовано",
    "refused": "❌ Отказ",
}
ALUM_STATUS = {
    "new": "⚪ Не связались",
    "contacted": "📞 Связались",
    "warm": "🤝 Тёплый контакт (подключён к компании)",
}
ROLES = ["Регистрация", "Фото/видео", "Логистика", "Работа на площадке"]

LETTER = (
    "Здравствуйте, {контактное лицо}!\n\n"
    "Студенческий совет Института железнодорожного транспорта (РУТ МИИТ) "
    "организует мероприятие «{название}» для студентов, которые в будущем "
    "станут специалистами транспортной отрасли.\n\n"
    "Предлагаем вашей компании стать партнёром: {формат поддержки}.\n"
    "Что получает компания: прямой выход на талантливых студентов "
    "(будущих сотрудников), упоминание в анонсах и на площадке, "
    "реализацию социальных и ESG-инициатив.\n\n"
    "Готовы обсудить детали в удобное для вас время.\n\n"
    "С уважением,\n{ФИО}, Студенческий совет ИЖТ"
)

router = Router()


# ---------- База данных ----------
def db(query: str, params: tuple = (), fetch: bool = False):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(query, params)
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows
    finally:
        con.close()


def init_db():
    db("""CREATE TABLE IF NOT EXISTS companies(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, sphere TEXT,
        contact TEXT, support TEXT, comment TEXT, status TEXT DEFAULT 'new',
        next_touch TEXT, tg_id INTEGER, created TEXT)""")
    db("""CREATE TABLE IF NOT EXISTS alumni(
        id INTEGER PRIMARY KEY AUTOINCREMENT, fio TEXT, year TEXT,
        job TEXT, ready TEXT, tg_id INTEGER, created TEXT)""")
    # новые поля для выпускников (на старой базе добавятся автоматически)
    for col in ("status TEXT DEFAULT 'new'", "owner TEXT"):
        try:
            db(f"ALTER TABLE alumni ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    db("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, date TEXT, need TEXT)""")
    db("""CREATE TABLE IF NOT EXISTS volunteers(
        id INTEGER PRIMARY KEY AUTOINCREMENT, fio TEXT, event_id INTEGER,
        role TEXT, tg_id INTEGER, created TEXT)""")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def is_admin(uid: int) -> bool:
    return uid in ADMINS


async def notify_admins(bot: Bot, text: str):
    for uid in ADMINS:
        try:
            await bot.send_message(uid, text)
        except Exception as e:  # админ мог не запускать бота
            logging.warning("Не удалось уведомить %s: %s", uid, e)


# ---------- Клавиатуры ----------
def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        for text, data in row:
            b.button(text=text, callback_data=data)
    b.adjust(*[len(r) for r in rows])
    return b.as_markup()


def main_menu() -> InlineKeyboardMarkup:
    return kb([
        [("🤝 Я компания, хочу стать партнёром", "m:company")],
        [("🎓 Я выпускник ИЖТ", "m:alumni")],
        [("🙋 Хочу помогать на мероприятиях", "m:vol")],
        [("🔒 Для студсовета", "m:admin")],
    ])


def admin_menu() -> InlineKeyboardMarkup:
    return kb([
        [("📋 Компании", "a:list"), ("📊 Воронка", "a:stats")],
        [("🎓 Выпускники", "a:alumni"), ("🙋 Волонтёры", "a:vols")],
        [("🔍 Подобрать партнёров", "a:find")],
        [("➕ Мероприятие", "a:addevent"), ("📝 Шаблон письма", "a:letter")],
        [("⬅️ В меню", "m:home")],
    ])


# ---------- Старт ----------
WELCOME = (
    "Привет! Это бот Студенческого совета ИЖТ (РУТ МИИТ).\n"
    "Мы организуем мероприятия для студентов и ищем партнёров. Выберите, кто вы:"
)


@router.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(WELCOME, reply_markup=main_menu())


@router.callback_query(F.data == "m:home")
async def home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer(WELCOME, reply_markup=main_menu())
    await cb.answer()


# ---------- Ветка: компания ----------
class CompanyForm(StatesGroup):
    name = State()
    sphere = State()
    contact = State()
    support = State()
    comment = State()


@router.callback_query(F.data == "m:company")
async def company_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer(
        "Студсовет ИЖТ даёт партнёрам доступ к талантливым студентам "
        "транспортной отрасли (будущим сотрудникам), упоминания в анонсах "
        "и возможность реализовать социальные и ESG-инициативы.\n\n"
        "Оставьте заявку. Как называется ваша компания?"
    )
    await state.set_state(CompanyForm.name)
    await cb.answer()


@router.message(CompanyForm.name)
async def company_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text)
    await msg.answer("Сфера деятельности компании?")
    await state.set_state(CompanyForm.sphere)


@router.message(CompanyForm.sphere)
async def company_sphere(msg: Message, state: FSMContext):
    await state.update_data(sphere=msg.text)
    await msg.answer("Контактное лицо и как с ним связаться (телефон, e-mail или Telegram)?")
    await state.set_state(CompanyForm.contact)


@router.message(CompanyForm.contact)
async def company_contact(msg: Message, state: FSMContext):
    await state.update_data(contact=msg.text)
    await msg.answer(
        "Какой формат поддержки вам ближе?",
        reply_markup=kb([[(v, f"sup:{k}")] for k, v in SUPPORT.items()]),
    )
    await state.set_state(CompanyForm.support)


@router.callback_query(CompanyForm.support, F.data.startswith("sup:"))
async def company_support(cb: CallbackQuery, state: FSMContext):
    await state.update_data(support=SUPPORT[cb.data.split(":")[1]])
    await cb.message.answer("Комментарий или пожелания (или напишите «-»)?")
    await state.set_state(CompanyForm.comment)
    await cb.answer()


@router.message(CompanyForm.comment)
async def company_done(msg: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    db(
        "INSERT INTO companies(name,sphere,contact,support,comment,tg_id,created)"
        " VALUES(?,?,?,?,?,?,?)",
        (d["name"], d["sphere"], d["contact"], d["support"], msg.text, msg.from_user.id, now()),
    )
    await state.clear()
    await msg.answer("Спасибо! Заявка передана студсовету, мы свяжемся с вами.",
                     reply_markup=kb([[("⬅️ В меню", "m:home")]]))
    await notify_admins(
        bot,
        f"🆕 Заявка от компании\n{d['name']} ({d['sphere']})\n"
        f"Контакт: {d['contact']}\nФормат: {d['support']}\nКомментарий: {msg.text}",
    )


# ---------- Ветка: выпускник ----------
class AlumniForm(StatesGroup):
    fio = State()
    year = State()
    job = State()
    ready = State()


@router.callback_query(F.data == "m:alumni")
async def alumni_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer(
        "Спасибо, что остаётесь на связи с институтом! Как вас зовут (ФИО)?"
    )
    await state.set_state(AlumniForm.fio)
    await cb.answer()


@router.message(AlumniForm.fio)
async def alumni_fio(msg: Message, state: FSMContext):
    await state.update_data(fio=msg.text)
    await msg.answer("Год выпуска?")
    await state.set_state(AlumniForm.year)


@router.message(AlumniForm.year)
async def alumni_year(msg: Message, state: FSMContext):
    await state.update_data(year=msg.text)
    await msg.answer("Где вы работаете сейчас (компания и должность)?")
    await state.set_state(AlumniForm.job)


@router.message(AlumniForm.job)
async def alumni_job(msg: Message, state: FSMContext):
    await state.update_data(job=msg.text)
    await msg.answer(
        "Готовы ли вы помочь совету контактом или рекомендацией в вашей компании?",
        reply_markup=kb([
            [("Да, готов(а)", "al:yes")],
            [("Возможно, обсудим", "al:maybe")],
            [("Пока нет", "al:no")],
        ]),
    )
    await state.set_state(AlumniForm.ready)


@router.callback_query(AlumniForm.ready, F.data.startswith("al:"))
async def alumni_done(cb: CallbackQuery, state: FSMContext, bot: Bot):
    ready = {"yes": "Да", "maybe": "Возможно", "no": "Пока нет"}[cb.data.split(":")[1]]
    d = await state.get_data()
    db(
        "INSERT INTO alumni(fio,year,job,ready,tg_id,created) VALUES(?,?,?,?,?,?)",
        (d["fio"], d["year"], d["job"], ready, cb.from_user.id, now()),
    )
    await state.clear()
    await cb.message.answer("Спасибо! Мы обязательно вам напишем.",
                            reply_markup=kb([[("⬅️ В меню", "m:home")]]))
    await notify_admins(bot, f"🎓 Новый выпускник: {d['fio']}, {d['year']}\n"
                             f"{d['job']}\nГотов помочь: {ready}")
    await cb.answer()


# ---------- Ветка: актив (волонтёры) ----------
class VolForm(StatesGroup):
    fio = State()


@router.callback_query(F.data == "m:vol")
async def vol_start(cb: CallbackQuery):
    events = db("SELECT * FROM events ORDER BY id DESC LIMIT 10", fetch=True)
    if not events:
        await cb.message.answer("Ближайших мероприятий пока нет, загляните позже!",
                                reply_markup=kb([[("⬅️ В меню", "m:home")]]))
    else:
        await cb.message.answer(
            "Выберите мероприятие:",
            reply_markup=kb([[(f"{e['title']} ({e['date']})", f"ev:{e['id']}")] for e in events]),
        )
    await cb.answer()


@router.callback_query(F.data.startswith("ev:"))
async def vol_event(cb: CallbackQuery, state: FSMContext):
    event_id = int(cb.data.split(":")[1])
    await state.update_data(event_id=event_id)
    await cb.message.answer(
        "Какую роль выберете?",
        reply_markup=kb([[(r, f"role:{i}")] for i, r in enumerate(ROLES)]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("role:"))
async def vol_role(cb: CallbackQuery, state: FSMContext):
    await state.update_data(role=ROLES[int(cb.data.split(":")[1])])
    await cb.message.answer("Как вас зовут (ФИО)?")
    await state.set_state(VolForm.fio)
    await cb.answer()


@router.message(VolForm.fio)
async def vol_done(msg: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    db(
        "INSERT INTO volunteers(fio,event_id,role,tg_id,created) VALUES(?,?,?,?,?)",
        (msg.text, d["event_id"], d["role"], msg.from_user.id, now()),
    )
    await state.clear()
    await msg.answer("Вы записаны! Спасибо, что помогаете институту 🙌",
                     reply_markup=kb([[("⬅️ В меню", "m:home")]]))
    await notify_admins(bot, f"🙋 Новый волонтёр: {msg.text}, роль: {d['role']}")


# ---------- Админ-панель ----------
class EventForm(StatesGroup):
    title = State()
    date = State()
    need = State()


@router.message(Command("admin"))
async def admin_cmd(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("Нет доступа.")
    await msg.answer("Панель студсовета:", reply_markup=admin_menu())


@router.callback_query(F.data == "m:admin")
async def admin_open(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.message.answer("Панель студсовета:", reply_markup=admin_menu())
    await cb.answer()


@router.callback_query(F.data == "a:list")
async def admin_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    rows = db("SELECT id,name,status FROM companies ORDER BY id DESC LIMIT 30", fetch=True)
    if not rows:
        await cb.message.answer("Компаний пока нет.")
    else:
        await cb.message.answer(
            "Компании (нажмите, чтобы открыть карточку):",
            reply_markup=kb([[(f"{STATUS[r['status']][:1]} {r['name']}", f"co:{r['id']}")] for r in rows]),
        )
    await cb.answer()


def card_text(c: sqlite3.Row) -> str:
    return (
        f"🏢 {c['name']}\nСфера: {c['sphere']}\nКонтакт: {c['contact']}\n"
        f"Формат: {c['support']}\nКомментарий: {c['comment']}\n"
        f"Статус: {STATUS[c['status']]}\n"
        f"Следующий контакт: {c['next_touch'] or '—'}"
    )


@router.callback_query(F.data.startswith("co:"))
async def company_card(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    cid = int(cb.data.split(":")[1])
    rows = db("SELECT * FROM companies WHERE id=?", (cid,), fetch=True)
    if not rows:
        return await cb.answer("Не найдено")
    await cb.message.answer(
        card_text(rows[0]),
        reply_markup=kb([[(v, f"st:{cid}:{k}")] for k, v in STATUS.items()]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("st:"))
async def set_status(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    _, cid, status = cb.data.split(":")
    next_touch = None
    if status in ("written", "talks"):
        next_touch = (datetime.now() + timedelta(days=REMIND_DAYS)).strftime("%Y-%m-%d %H:%M")
    db("UPDATE companies SET status=?, next_touch=? WHERE id=?", (status, next_touch, int(cid)))
    extra = f"\nНапомню через {REMIND_DAYS} дн." if next_touch else ""
    await cb.answer(f"Статус: {STATUS[status]}{extra}", show_alert=True)


@router.callback_query(F.data == "a:stats")
async def admin_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    lines = []
    for k, v in STATUS.items():
        n = db("SELECT COUNT(*) AS n FROM companies WHERE status=?", (k,), fetch=True)[0]["n"]
        lines.append(f"{v}: {n}")
    await cb.message.answer("📊 Воронка партнёров\n" + "\n".join(lines))
    await cb.answer()


@router.callback_query(F.data == "a:alumni")
async def admin_alumni(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    rows = db("SELECT * FROM alumni ORDER BY id DESC LIMIT 30", fetch=True)
    if not rows:
        await cb.message.answer("Выпускников пока нет.")
    else:
        await cb.message.answer(
            "🎓 Выпускники (нажмите, чтобы открыть карточку):",
            reply_markup=kb([
                [(f"{ALUM_STATUS[r['status'] or 'new'][:1]} {r['fio']} ({r['year']})", f"alc:{r['id']}")]
                for r in rows
            ]),
        )
    await cb.answer()


def alumni_card(a: sqlite3.Row) -> str:
    return (
        f"🎓 {a['fio']} ({a['year']})\nРаботает: {a['job']}\n"
        f"Готов помочь: {a['ready']}\n"
        f"Статус: {ALUM_STATUS[a['status'] or 'new']}\n"
        f"Ответственный: {a['owner'] or '—'}"
    )


def alumni_kb(aid: int) -> InlineKeyboardMarkup:
    rows = [[(v, f"als:{aid}:{k}")] for k, v in ALUM_STATUS.items()]
    rows.append([("🙋 Я ответственный", f"alo:{aid}")])
    return kb(rows)


@router.callback_query(F.data.startswith("alc:"))
async def alumni_open(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    aid = int(cb.data.split(":")[1])
    rows = db("SELECT * FROM alumni WHERE id=?", (aid,), fetch=True)
    if not rows:
        return await cb.answer("Не найдено")
    await cb.message.answer(alumni_card(rows[0]), reply_markup=alumni_kb(aid))
    await cb.answer()


@router.callback_query(F.data.startswith("als:"))
async def alumni_status(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    _, aid, status = cb.data.split(":")
    db("UPDATE alumni SET status=? WHERE id=?", (status, int(aid)))
    await cb.answer(f"Статус: {ALUM_STATUS[status]}", show_alert=True)


@router.callback_query(F.data.startswith("alo:"))
async def alumni_owner(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    aid = int(cb.data.split(":")[1])
    db("UPDATE alumni SET owner=? WHERE id=?", (cb.from_user.full_name, aid))
    await cb.answer(f"Ответственный: {cb.from_user.full_name}", show_alert=True)


@router.callback_query(F.data == "a:vols")
async def admin_vols(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    rows = db(
        "SELECT v.fio, v.role, e.title FROM volunteers v "
        "LEFT JOIN events e ON e.id=v.event_id ORDER BY v.id DESC LIMIT 40",
        fetch=True,
    )
    text = "🙋 Волонтёры\n\n" + "\n".join(
        f"{r['fio']} — {r['role']} ({r['title']})" for r in rows
    ) if rows else "Волонтёров пока нет."
    await cb.message.answer(text)
    await cb.answer()


@router.callback_query(F.data == "a:letter")
async def admin_letter(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.answer("📝 Шаблон письма (подставьте данные в {фигурных скобках}):\n\n" + LETTER)
    await cb.answer()


@router.callback_query(F.data == "a:addevent")
async def event_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.answer("Название мероприятия?")
    await state.set_state(EventForm.title)
    await cb.answer()


@router.message(EventForm.title)
async def event_title(msg: Message, state: FSMContext):
    await state.update_data(title=msg.text)
    await msg.answer("Дата проведения?")
    await state.set_state(EventForm.date)


@router.message(EventForm.date)
async def event_date(msg: Message, state: FSMContext):
    await state.update_data(date=msg.text)
    await msg.answer("Что нужно от партнёров (призы, площадка, спикер...)?")
    await state.set_state(EventForm.need)


@router.message(EventForm.need)
async def event_done(msg: Message, state: FSMContext):
    d = await state.get_data()
    db("INSERT INTO events(title,date,need) VALUES(?,?,?)", (d["title"], d["date"], msg.text))
    await state.clear()
    await msg.answer("Мероприятие добавлено ✅", reply_markup=admin_menu())


# ---------- Подбор партнёров (ИИ) ----------
# smertapi.top — OpenAI-совместимый API, один ключ ко многим моделям
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://smertapi.top/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "kimi-k3")


class FindForm(StatesGroup):
    event = State()
    audience = State()
    need = State()
    paste = State()


def build_prompt(d: dict) -> str:
    return (
        "Ты помогаешь студенческому совету Института железнодорожного транспорта "
        "РУТ (МИИТ) найти партнёров для мероприятия.\n"
        f"Мероприятие: {d['event']}\nАудитория: {d['audience']}\n"
        f"Что нужно от партнёров: {d['need']}\n\n"
        "Подбери 10 реальных российских компаний (транспорт, логистика, ж/д, IT, "
        "промышленность и смежные сферы), которым это мероприятие может быть интересно "
        "(доступ к студентам, рекрутинг, ESG). Только реально существующие компании, "
        "ничего не выдумывай, а если не уверен в сайте, напиши «проверить». "
        "Ответ дай по одной компании в строке в формате:\n"
        "Название | сфера | почему подходит и что можно попросить | сайт\n"
        "Без таблиц, заголовков и пояснений до или после списка."
    )


def _ai_error(status: int, body: str) -> str:
    try:
        msg = json.loads(body)["error"]["message"]
    except Exception:
        msg = body[:300]
    if status == 401:
        return "ключ AI_API_KEY не принят сервисом — проверьте, что он скопирован полностью"
    if status == 402:
        return "на балансе smertapi.top закончились средства"
    if status in (400, 404) and "model" in msg.lower():
        return f"модель «{AI_MODEL}» недоступна ({msg}). Список моделей: /models"
    return f"ошибка сервиса ИИ {status}: {msg}"


async def ai_request(method: str, path: str, payload: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {AI_API_KEY}"}
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.request(method, AI_BASE_URL + path, json=payload, headers=headers) as r:
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(_ai_error(r.status, body))
            return json.loads(body)


async def ask_ai(prompt: str) -> list[dict]:
    data = await ai_request("POST", "/chat/completions", {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
    })
    items = parse_lines(data["choices"][0]["message"]["content"] or "")
    if not items:
        raise RuntimeError("ИИ ответил не в нужном формате, попробуйте ещё раз")
    return items[:10]


@router.message(Command("models"))
async def models_cmd(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    if not AI_API_KEY:
        return await msg.answer("Не задан AI_API_KEY")
    try:
        data = await ai_request("GET", "/models")
    except Exception as e:
        return await msg.answer(f"Не удалось получить список моделей: {e}")
    ids = sorted(m["id"] for m in data.get("data", []))
    text = f"Сейчас используется: {AI_MODEL}\nДоступные модели ({len(ids)}):\n" + "\n".join(ids)
    for i in range(0, len(text), 4000):
        await msg.answer(text[i:i + 4000])


def parse_lines(text: str) -> list[dict]:
    items = []
    for line in text.splitlines():
        # модели часто отвечают markdown-таблицей или с **жирным** — приводим к «a | b | c | d»
        line = line.replace("**", "").replace("`", "").strip().strip("|").strip()
        line = re.sub(r"^(\d+[.)]|[-•*])\s*", "", line)
        parts = [p.strip() for p in line.split("|")]
        if not parts[0] or set(parts[0]) <= set("-: ") or parts[0].lower() in ("название", "компания", "№"):
            continue
        if len(parts) >= 2:
            items.append({
                "name": parts[0], "sphere": parts[1],
                "why": parts[2] if len(parts) > 2 else "",
                "site": parts[3] if len(parts) > 3 else "",
            })
    return items


def save_items(items: list[dict], need: str) -> int:
    for it in items:
        db(
            "INSERT INTO companies(name,sphere,contact,support,comment,created)"
            " VALUES(?,?,?,?,?,?)",
            (it.get("name", ""), it.get("sphere", ""), it.get("site") or "не указан",
             need, it.get("why", ""), now()),
        )
    return len(items)


@router.callback_query(F.data == "a:find")
async def find_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.answer("Для какого мероприятия ищем партнёров? Название и тема.")
    await state.set_state(FindForm.event)
    await cb.answer()


@router.message(FindForm.event)
async def find_event(msg: Message, state: FSMContext):
    await state.update_data(event=msg.text)
    await msg.answer("Кто аудитория? (например: студенты 1-2 курсов, около 100 человек)")
    await state.set_state(FindForm.audience)


@router.message(FindForm.audience)
async def find_audience(msg: Message, state: FSMContext):
    await state.update_data(audience=msg.text)
    await msg.answer("Что нужно от партнёров? (призы, площадка, спикер, стажировки...)")
    await state.set_state(FindForm.need)


@router.message(FindForm.need)
async def find_need(msg: Message, state: FSMContext):
    await state.update_data(need=msg.text)
    d = await state.get_data()
    prompt = build_prompt(d)
    if not AI_API_KEY:
        await msg.answer("Скопируйте запрос ниже в любой ИИ (Claude, ChatGPT и т.д.):")
        await msg.answer(prompt)
        await msg.answer(
            "Потом пришлите сюда ответ ИИ: по одной компании в строке, формат:\n"
            "Название | сфера | почему подходит | сайт"
        )
        await state.set_state(FindForm.paste)
        return
    await msg.answer("Ищу компании, это может занять до пары минут...")
    try:
        items = await ask_ai(prompt)
    except Exception as e:
        await state.clear()
        return await msg.answer(f"Не удалось получить подборку: {e}", reply_markup=admin_menu())
    await state.update_data(items=items)
    await state.set_state(None)  # данные остаются для кнопки «Добавить все»
    lines = [
        f"{i}. {it.get('name')} ({it.get('sphere')})\n   {it.get('why')}\n   {it.get('site')}"
        for i, it in enumerate(items, 1)
    ]
    await msg.answer(
        "🔍 Подборка партнёров\n\n" + "\n\n".join(lines) +
        "\n\n⚠️ ИИ может ошибаться: проверьте компании и контакты по сайтам.",
        reply_markup=kb([[("➕ Добавить все в базу", "f:save")], [("Отмена", "m:admin")]]),
    )


@router.callback_query(F.data == "f:save")
async def find_save(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    d = await state.get_data()
    items = d.get("items")
    if not items:
        return await cb.answer("Подборка устарела, запросите заново", show_alert=True)
    n = save_items(items, d.get("need", ""))
    await state.clear()
    await cb.message.answer(f"Добавлено компаний в базу: {n} ✅ (раздел «Компании»)")
    await cb.answer()


@router.message(FindForm.paste)
async def find_paste(msg: Message, state: FSMContext):
    items = parse_lines(msg.text or "")
    if not items:
        return await msg.answer("Не вижу строк формата: Название | сфера | почему подходит | сайт")
    d = await state.get_data()
    n = save_items(items, d.get("need", ""))
    await state.clear()
    await msg.answer(f"Добавлено компаний в базу: {n} ✅", reply_markup=admin_menu())


# ---------- Напоминания о повторном контакте ----------
async def reminder_loop(bot: Bot):
    while True:
        try:
            due = db(
                "SELECT * FROM companies WHERE next_touch IS NOT NULL "
                "AND next_touch <= ? AND status IN ('written','talks')",
                (now(),), fetch=True,
            )
            for c in due:
                await notify_admins(
                    bot, f"⏰ Пора напомнить о себе: {c['name']}\nСтатус: {STATUS[c['status']]}\n"
                         f"Контакт: {c['contact']}"
                )
                nxt = (datetime.now() + timedelta(days=REMIND_DAYS)).strftime("%Y-%m-%d %H:%M")
                db("UPDATE companies SET next_touch=? WHERE id=?", (nxt, c["id"]))
        except Exception as e:
            logging.warning("Ошибка напоминаний: %s", e)
        await asyncio.sleep(3600)


async def weekly_loop(bot: Bot):
    last = None
    while True:
        n = datetime.now()
        if n.weekday() == 0 and n.hour >= 10 and last != n.date():
            last = n.date()
            await notify_admins(
                bot, "📅 Новая неделя: пора подобрать партнёров. "
                     "Откройте /admin → «🔍 Подобрать партнёров»."
            )
        await asyncio.sleep(1800)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN")
    init_db()
    logging.info("ИИ-подбор: %s", f"{AI_BASE_URL}, модель {AI_MODEL}" if AI_API_KEY else "выключен (нет AI_API_KEY)")
    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
    bot = Bot(BOT_TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(reminder_loop(bot))
    asyncio.create_task(weekly_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
