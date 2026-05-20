"""
Команды:
/start — стоимость 0 энергии — регистрация игрока и приветствие
/profile — стоимость 0 энергии — показать HP, энергию, уровень, XP и золото
/attack @username — стоимость 20 энергии — атаковать игрока, нанести урон, получить добычу/XP при победе
/heal — стоимость 10 энергии — восстановить 30 HP, не выше max_hp
/top — стоимость 0 энергии — показать топ-5 игроков по уровню и XP
/energy — стоимость 0 энергии — показать энергию и время до следующей регенерации
"""

import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes


DB_PATH = Path(__file__).with_name("game.db")
MAX_ENERGY = 100
ATTACK_COST = 20
HEAL_COST = 10
ATTACK_COOLDOWN = 60


# --- DB init ---
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                hp INTEGER DEFAULT 100,
                max_hp INTEGER DEFAULT 100,
                energy INTEGER DEFAULT 100,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                gold INTEGER DEFAULT 50,
                last_attack REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attack_cooldowns (
                attacker_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                last_attack REAL NOT NULL,
                PRIMARY KEY (attacker_id, target_id)
            )
            """
        )


def get_user(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def get_user_by_username(username: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE lower(username) = ? LIMIT 1", (username.lower(),)
        ).fetchone()


def upsert_user(user_id: int, username: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """,
            (user_id, username),
        )


def refresh_username(user_id: int, username: str | None) -> sqlite3.Row | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        if row["username"] != username:
            conn.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row


async def require_user(update: Update) -> sqlite3.Row | None:
    tg_user = update.effective_user
    message = update.effective_message
    if tg_user is None or message is None:
        return None
    row = refresh_username(tg_user.id, tg_user.username)
    if row is None:
        await message.reply_text("Сначала используй /start")
    return row


def display_name(row: sqlite3.Row) -> str:
    return f"@{row['username']}" if row["username"] else f"id{row['user_id']}"


def next_level_xp(row: sqlite3.Row) -> int:
    return max(0, row["level"] * 100 - row["xp"])


def apply_level_ups(xp: int, level: int, max_hp: int) -> tuple[int, int, int]:
    gained = 0
    while xp >= level * 100:
        level += 1
        max_hp += 20
        gained += 1
    return level, max_hp, gained


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    message = update.effective_message
    if tg_user is None or message is None:
        return
    upsert_user(tg_user.id, tg_user.username)
    await message.reply_text(
        "Ты зарегистрирован. Используй /profile, /attack @username, /heal, /top и /energy."
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_user(update)
    if user is None or update.effective_message is None:
        return
    await update.effective_message.reply_text(
        f"Профиль {display_name(user)}\n"
        f"HP: {user['hp']}/{user['max_hp']}\n"
        f"Энергия: {user['energy']}/{MAX_ENERGY}\n"
        f"Уровень: {user['level']}\n"
        f"XP: {user['xp']} (до уровня: {next_level_xp(user)})\n"
        f"Золото: {user['gold']}"
    )


async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    attacker = await require_user(update)
    tg_user = update.effective_user
    message = update.effective_message
    if attacker is None or tg_user is None or message is None:
        return
    if not context.args or not context.args[0].startswith("@"):
        await message.reply_text("Используй: /attack @username")
        return

    target_username = context.args[0].lstrip("@").lower()
    if tg_user.username and target_username == tg_user.username.lower():
        await message.reply_text("Нельзя атаковать себя")
        return

    target = get_user_by_username(target_username)
    if target is None:
        await message.reply_text("Игрок не найден")
        return
    if target["user_id"] == attacker["user_id"]:
        await message.reply_text("Нельзя атаковать себя")
        return
    if attacker["energy"] < ATTACK_COST:
        await message.reply_text(
            f"Недостаточно энергии (нужно {ATTACK_COST}, есть {attacker['energy']})"
        )
        return

    now = time.time()
    with get_conn() as conn:
        cooldown = conn.execute(
            """
            SELECT last_attack FROM attack_cooldowns
            WHERE attacker_id = ? AND target_id = ?
            """,
            (attacker["user_id"], target["user_id"]),
        ).fetchone()
    if cooldown is not None and now - cooldown["last_attack"] < ATTACK_COOLDOWN:
        remaining = ceil(ATTACK_COOLDOWN - (now - cooldown["last_attack"]))
        await message.reply_text(f"Подожди ещё {remaining} секунд")
        return

    damage = random.randint(10, 20) + (attacker["level"] * 2)
    new_target_hp = target["hp"] - damage
    defeated = new_target_hp <= 0
    stolen_gold = min(10, target["gold"]) if defeated else 0
    gained_xp = 25 if defeated else 0
    new_energy = attacker["energy"] - ATTACK_COST
    new_xp = attacker["xp"] + gained_xp
    new_level, new_max_hp, level_gained = apply_level_ups(
        new_xp, attacker["level"], attacker["max_hp"]
    )

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET energy = ?, gold = gold + ?, xp = ?, level = ?, max_hp = ?, last_attack = ?
            WHERE user_id = ?
            """,
            (
                new_energy,
                stolen_gold,
                new_xp,
                new_level,
                new_max_hp,
                now,
                attacker["user_id"],
            ),
        )
        conn.execute(
            """
            INSERT INTO attack_cooldowns (attacker_id, target_id, last_attack)
            VALUES (?, ?, ?)
            ON CONFLICT(attacker_id, target_id) DO UPDATE SET last_attack = excluded.last_attack
            """,
            (attacker["user_id"], target["user_id"], now),
        )
        if defeated:
            conn.execute(
                "UPDATE users SET hp = 50, gold = gold - ? WHERE user_id = ?",
                (stolen_gold, target["user_id"]),
            )
        else:
            conn.execute(
                "UPDATE users SET hp = ? WHERE user_id = ?",
                (new_target_hp, target["user_id"]),
            )

    if defeated:
        text = (
            f"Ты победил {display_name(target)} и забрал {stolen_gold} золота. "
            f"+{gained_xp} XP. Цель возрождена с 50 HP. Энергия: {new_energy}/{MAX_ENERGY}"
        )
    else:
        text = (
            f"Ты атаковал {display_name(target)} и нанёс {damage} урона. "
            f"HP цели: {new_target_hp}/{target['max_hp']}. Энергия: {new_energy}/{MAX_ENERGY}"
        )
    if level_gained:
        text += f"\nНовый уровень: {new_level}. Макс. HP увеличен на {level_gained * 20}."
    await message.reply_text(text)


async def heal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_user(update)
    message = update.effective_message
    if user is None or message is None:
        return
    if user["hp"] >= user["max_hp"]:
        await message.reply_text("HP уже полное")
        return
    if user["energy"] < HEAL_COST:
        await message.reply_text(f"Недостаточно энергии (нужно {HEAL_COST}, есть {user['energy']})")
        return

    new_hp = min(user["max_hp"], user["hp"] + 30)
    new_energy = user["energy"] - HEAL_COST
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET hp = ?, energy = ? WHERE user_id = ?",
            (new_hp, new_energy, user["user_id"]),
        )
    await message.reply_text(f"Ты восстановил HP до {new_hp}/{user['max_hp']}. Энергия: {new_energy}/{MAX_ENERGY}")


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY level DESC, xp DESC LIMIT 5"
        ).fetchall()
    if not rows:
        await message.reply_text("Топ пуст")
        return
    lines = ["Топ игроков:"]
    for index, row in enumerate(rows, 1):
        lines.append(f"{index}. {display_name(row)} — уровень {row['level']}, XP {row['xp']}, золото {row['gold']}")
    await message.reply_text("\n".join(lines))


async def energy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_user(update)
    message = update.effective_message
    if user is None or message is None:
        return
    scheduler: AsyncIOScheduler | None = context.application.bot_data.get("scheduler")
    next_text = "не запланирована"
    if scheduler is not None:
        job = scheduler.get_job("energy_regen")
        if job and job.next_run_time:
            now = datetime.now(job.next_run_time.tzinfo)
            seconds = max(0, int((job.next_run_time - now).total_seconds()))
            next_text = f"через {seconds // 60} мин {seconds % 60} сек"
    await message.reply_text(f"Энергия: {user['energy']}/{MAX_ENERGY}\nСледующая регенерация: {next_text}")


# --- Scheduler ---
async def regenerate_energy() -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET energy = MIN(energy + 10, ?) WHERE energy < ?",
            (MAX_ENERGY, MAX_ENERGY),
        )


async def start_scheduler(application: Application) -> None:
    scheduler: AsyncIOScheduler = application.bot_data["scheduler"]
    if not scheduler.running:
        scheduler.start()


async def stop_scheduler(application: Application) -> None:
    scheduler: AsyncIOScheduler | None = application.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)


def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.environ.get("BOT_TOKEN")
    if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN не задан")

    init_db()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        regenerate_energy,
        "interval",
        minutes=30,
        id="energy_regen",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=30),
    )

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(start_scheduler)
        .post_shutdown(stop_scheduler)
        .build()
    )
    app.bot_data["scheduler"] = scheduler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("attack", attack))
    app.add_handler(CommandHandler("heal", heal))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("energy", energy))
    app.run_polling()


if __name__ == "__main__":
    main()
