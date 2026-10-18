"""Discord-бот: держит голосовой канал 24/7.

Что умеет:
  * при запуске сам заходит в канал из CHANNEL_ID и остаётся там;
  * возвращается, если его кикнули из голоса;
  * возвращает себя в свой канал, если его перетащили в другой;
  * команды /join, /leave, /status.

Запуск:  python bot.py
Настройки — в файле .env (см. .env.example).
"""

import asyncio
import logging
import os
import time
from collections import deque

import discord
from discord.ext import commands

# ------------------------- Настройки -------------------------


def _load_env(path: str = ".env") -> None:
    """Прочитать .env в os.environ. Переменные из панели хостинга имеют
    приоритет — setdefault их не перезапишет."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env()

TOKEN = os.environ.get("TOKEN", "").strip()
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0") or 0)

MAX_ATTEMPTS = 20        # попыток входа подряд, после — сдаёмся
RETRY_DELAY = 5          # пауза между попытками, сек
REJOIN_DELAY = 3         # пауза перед возвратом после кика, сек
MOVE_BACK_WINDOW = 300   # окно анти-«перетаскиваний», сек
MAX_MOVE_BACKS = 5       # столько возвратов в 5 минут, потом не мешаем

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# Минимальные интенты: ни текстовых сообщений, ни списка участников.
intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True

# command_prefix — формальность, работают только slash-команды.
bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    help_command=None,
)

# ------------------------- Состояние -------------------------

START_TIME = time.monotonic()
want_voice = bool(CHANNEL_ID)   # должен ли бот сидеть в канале
manual_leave = False            # выход по /leave — не возвращаться
started = False
attempts = 0
reconnects = 0
ever_connected = False
session_started: float | None = None
move_backs: deque[float] = deque()


# ------------------------- Голос -------------------------


def _config_channel():
    """Целевой канал из CHANNEL_ID (или None)."""
    if not CHANNEL_ID:
        return None
    ch = bot.get_channel(CHANNEL_ID)
    return ch if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) else None


def _connected(ch) -> bool:
    if ch is None:
        return False
    vc = ch.guild.voice_client
    return isinstance(vc, discord.VoiceClient) and vc.is_connected() and vc.channel.id == ch.id


async def _connect(channel) -> discord.VoiceClient:
    """Подключиться к каналу (или переехать в него, если уже подключены)."""
    guild = channel.guild
    vc = guild.voice_client
    if isinstance(vc, discord.VoiceClient) and vc.is_connected():
        if vc.channel.id != channel.id:
            await vc.move_to(channel)
        return vc
    if vc is not None and not vc.is_connected():
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
    return await channel.connect(self_deaf=True)


def _note_connected() -> None:
    """Отметить, что бот на связи (для /status)."""
    global attempts, session_started, ever_connected, reconnects
    attempts = 0
    if session_started is not None:
        return
    if ever_connected:
        reconnects += 1
    else:
        ever_connected = True
    session_started = time.monotonic()


async def _ensure_in_voice():
    """Держать бота в целевом канале. None — если не получилось."""
    global attempts
    ch = _config_channel()
    if ch is None:
        return None
    if _connected(ch):
        _note_connected()
        return ch.guild.voice_client

    while attempts < MAX_ATTEMPTS:
        try:
            vc = await _connect(ch)
        except Exception as exc:
            attempts += 1
            log.warning("Вход в канал не удался (%s/%s): %s", attempts, MAX_ATTEMPTS, exc)
            await asyncio.sleep(RETRY_DELAY)
            continue
        _note_connected()
        log.info("В канале: %s", ch.name)
        return vc

    log.warning("Не смог войти в канал за %s попыток — проверь права бота", MAX_ATTEMPTS)
    return None


def _allow_move_back() -> bool:
    """Не даём бесконечно бороться, если бота перетаскивают специально."""
    now = time.monotonic()
    while move_backs and now - move_backs[0] > MOVE_BACK_WINDOW:
        move_backs.popleft()
    if len(move_backs) >= MAX_MOVE_BACKS:
        return False
    move_backs.append(now)
    return True


# ------------------------- Вспомогательное -------------------------


async def reply(interaction: discord.Interaction, text: str, ephemeral: bool = False) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(text, ephemeral=ephemeral)
    except discord.HTTPException:
        pass


def _fmt_time(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {secs} с"
    return f"{secs} с"


# ------------------------- События -------------------------


@bot.event
async def on_ready() -> None:
    global started
    log.info("Запущен как %s", bot.user)

    if started:
        return
    started = True

    try:
        await bot.tree.sync()
        ch = _config_channel()
        if ch is not None:
            bot.tree.copy_global_to(guild=ch.guild)
            await bot.tree.sync(guild=ch.guild)
        log.info("Slash-команды синхронизированы")
    except Exception as exc:
        log.warning("Синхронизация команд не удалась: %s", exc)

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="лобби 24/7")
    )

    if not CHANNEL_ID:
        log.warning("CHANNEL_ID не задан — бот будет ждать команду /join")
        return
    if _config_channel() is None:
        log.warning("CHANNEL_ID=%s не найден или это не голосовой канал", CHANNEL_ID)
        return
    await _ensure_in_voice()


@bot.event
async def on_voice_state_update(member: discord.Member, before, after) -> None:
    """Кикнули — вернуться. Перетащили в чужой канал — вернуть себя."""
    global session_started
    if bot.user is None or member.id != bot.user.id:
        return
    if manual_leave or not want_voice:
        return

    guild = member.guild
    target = _config_channel()

    # 1) Бот в другом канале — вернуть в свой
    if after.channel is not None:
        if target is None or after.channel.id == target.id:
            return
        await asyncio.sleep(2)
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel.id == target.id:
            return
        if not _allow_move_back():
            log.warning("Бота перетаскивают слишком часто — остаюсь где поставили")
            return
        try:
            await vc.move_to(target)
            log.info("Вернул себя в %s", target.name)
        except Exception as exc:
            log.warning("Не смог вернуться в канал: %s", exc)
        return

    # 2) Бота выкинули из голоса — вернуться
    session_started = None
    await asyncio.sleep(REJOIN_DELAY)
    if _config_channel() is None:
        return
    vc = guild.voice_client
    if vc is not None and vc.is_connected():
        return
    await _ensure_in_voice()


# ------------------------- Команды -------------------------


@bot.tree.command(name="join", description="Завести бота в голосовой канал")
async def cmd_join(interaction: discord.Interaction) -> None:
    global want_voice, manual_leave
    manual_leave = False
    want_voice = True
    move_backs.clear()
    vc = await _ensure_in_voice()
    if vc is None:
        return await reply(
            interaction,
            "❌ Не смог зайти в канал — проверь право «Подключение» и CHANNEL_ID",
            ephemeral=True,
        )
    await reply(interaction, f"✅ Держу канал **{vc.channel.name}**")


@bot.tree.command(name="leave", description="Вывести бота из канала")
async def cmd_leave(interaction: discord.Interaction) -> None:
    global want_voice, manual_leave, session_started
    want_voice = False
    manual_leave = True
    session_started = None
    vc = interaction.guild.voice_client if interaction.guild else None
    if isinstance(vc, discord.VoiceClient) and vc.is_connected():
        await vc.disconnect()
    await reply(interaction, "👋 Вышел из канала. Позовёшь — вернусь (/join)")


@bot.tree.command(name="status", description="Показать состояние держателя канала")
async def cmd_status(interaction: discord.Interaction) -> None:
    ch = _config_channel()
    vc = interaction.guild.voice_client if interaction.guild else None
    lines = [f"Целевой канал: **{ch.name}**" if ch else "Целевой канал: — (не задан CHANNEL_ID)"]

    if isinstance(vc, discord.VoiceClient) and vc.is_connected():
        lines.append(f"Состояние: 🟢 держу **{vc.channel.name}**")
        if session_started is not None:
            lines.append(f"Без обрывов: {_fmt_time(time.monotonic() - session_started)}")
        try:
            lines.append(f"Пинг голоса: {vc.latency * 1000:.0f} ms")
        except Exception:
            pass
    else:
        lines.append("Состояние: 🔴 не в канале" + ("" if want_voice else " (после /leave)"))

    lines.append(f"Переподключений: {reconnects}")
    lines.append(f"Бот работает: {_fmt_time(time.monotonic() - START_TIME)}")
    await reply(interaction, "\n".join(lines))


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: Exception) -> None:
    log.error("Ошибка команды: %s", error, exc_info=error)
    await reply(interaction, f"⚠️ {error}", ephemeral=True)


# ------------------------- Запуск -------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Не задан TOKEN в .env")
    try:
        # log_handler=None: пишем в лог через basicConfig, без дублей
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("Неверный токен бота — проверь .env")
