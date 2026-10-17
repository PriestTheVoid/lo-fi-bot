"""Discord-бот: держит голосовой канал 24/7 и играет музыку (лo-fi).

Запуск:  python bot.py
Переменные окружения читаются из файла .env (см. .env.example).
"""

import asyncio
import logging
import os
import re
from collections import deque

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

# ------------------------- Настройки -------------------------

def _load_env(path: str = ".env") -> None:
    """Прочитать .env в os.environ. Уже заданные переменные (из панели
    хостинга) имеют приоритет — setdefault их не перезапишет."""
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
VOLUME = float(os.environ.get("VOLUME", "0.6") or 0.6)
MAX_QUEUE = 50
MAX_REJOIN_ATTEMPTS = 20

# ffmpeg: переподключение к потоку + тихий лог.
# Формат (-f s16le, 48 kHz, стерео) discord.py добавляет сам — не дублируем.
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 10 -nostdin"
FFMPEG_OPTS = "-vn -loglevel error"

# yt-dlp: только аудио, без плейлистов и без прогресс-бара — минимум работы.
YTDL_OPTS = {
    "format": "bestaudio",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "skip_download": True,
    "default_search": "auto",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# Минимальные интенты: боту не нужны ни текстовые сообщения, ни список участников.
intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True

# command_prefix — чисто формальность, префиксные команды не используются,
# работают только slash-команды (when_mentioned не требует message_content).
bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    help_command=None,
)

# ------------------------- Состояние -------------------------

queue: deque[dict] = deque()
current: dict | None = None
want_voice = bool(CHANNEL_ID)   # должен ли бот сидеть в канале
manual_leave = False            # бот вышел сам по /leave — не возвращаться
rejoin_attempts = 0
started = False
player_task: asyncio.Task | None = None


# ------------------------- Поиск и источник -------------------------

def _extract(query: str) -> dict:
    """Синхронный (в отдельном потоке) поиск/разбор ссылки через yt-dlp."""
    url = query.strip()
    if not re.match(r"^https?://", url, re.I):
        url = f"ytsearch1:{url}"

    with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise RuntimeError("ничего не найдено")

    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RuntimeError("ничего не найдено")
        info = entries[0]

    stream = info.get("url")
    if not stream:
        req = info.get("requested_formats") or []
        if req:
            stream = req[0].get("url")
    if not stream:
        raise RuntimeError("не удалось получить ссылку на аудио")

    return {
        "title": info.get("title") or url,
        "stream": stream,
        "is_live": bool(info.get("is_live")),
    }


def _make_source(track: dict) -> discord.FFmpegPCMAudio:
    return discord.FFmpegPCMAudio(
        track["stream"],
        before_options=FFMPEG_BEFORE,
        options=FFMPEG_OPTS,
    )


# ------------------------- Подключение к голосу -------------------------

def _config_channel():
    if not CHANNEL_ID:
        return None
    ch = bot.get_channel(CHANNEL_ID)
    return ch if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) else None


async def _connect(channel) -> discord.VoiceClient:
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


async def _reconnect(guild: discord.Guild):
    ch = _config_channel()
    if ch is None:
        return None
    stale = guild.voice_client
    if stale is not None and not stale.is_connected():
        try:
            await stale.disconnect(force=True)
        except Exception:
            pass
    try:
        return await ch.connect(self_deaf=True)
    except Exception:
        return None


# ------------------------- Плеер -------------------------

def ensure_player(guild: discord.Guild) -> None:
    global player_task
    if player_task is None or player_task.done():
        player_task = asyncio.create_task(player_loop(guild))


async def player_loop(guild: discord.Guild) -> None:
    global current
    reconnect_fails = 0
    while want_voice and not manual_leave:
        if not queue:
            break

        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            vc = await _reconnect(guild)
            if vc is None:
                reconnect_fails += 1
                if reconnect_fails > MAX_REJOIN_ATTEMPTS:
                    log.warning("Канал недоступен — плеер остановлен")
                    break
                await asyncio.sleep(5)
                continue
        reconnect_fails = 0

        track = queue.popleft()
        current = track
        try:
            # ffmpeg запускается в отдельном потоке, чтобы не блокировать цикл
            src = await asyncio.to_thread(_make_source, track)
            wrapped = discord.PCMVolumeTransformer(src, volume=VOLUME)
        except Exception as exc:
            log.warning("Не удалось открыть поток %s: %s", track["title"], exc)
            current = None
            continue

        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _after(_err, _loop=loop, _d=done):
            try:
                _loop.call_soon_threadsafe(_d.set)
            except Exception:
                pass

        try:
            vc.play(wrapped, after=_after)
        except Exception as exc:
            log.warning("Не удалось запустить воспроизведение: %s", exc)
            current = None
            continue

        await done.wait()

        # live-радио (лоу-фай 24/7) — перезапускаем по кругу, пока бот в канале
        if track.get("is_live") and want_voice and not manual_leave:
            await asyncio.sleep(2)
            queue.append(track)

        current = None


# ------------------------- Вспомогательное -------------------------

async def reply(interaction: discord.Interaction, text: str, ephemeral: bool = False) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(text, ephemeral=ephemeral)
    except discord.HTTPException:
        pass


def _voice_channel_for(interaction: discord.Interaction):
    user = interaction.user
    voice = getattr(user, "voice", None)
    if voice and voice.channel:
        return voice.channel
    return _config_channel()


# ------------------------- События -------------------------

def guild_connected(ch) -> bool:
    vc = ch.guild.voice_client
    return isinstance(vc, discord.VoiceClient) and vc.is_connected()


@bot.event
async def on_ready() -> None:
    global started, rejoin_attempts
    log.info("Запущен как %s", bot.user)

    if started:
        return
    started = True

    # Команды: глобальная синхронизация + копия в гильдию бота,
    # чтобы slash-команды появились на сервере сразу после запуска
    try:
        await bot.tree.sync()
        ch = _config_channel()
        if ch is not None:
            bot.tree.copy_global_to(guild=ch.guild)
            await bot.tree.sync(guild=ch.guild)
        log.info("Slash-команды синхронизированы")
    except Exception as exc:
        log.warning("Не удалось синхронизировать команды: %s", exc)

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="/play")
    )

    if CHANNEL_ID:
        ch = _config_channel()
        if ch is None:
            log.warning("CHANNEL_ID=%s не найден (голосовой канал не существует?)", CHANNEL_ID)
        else:
            for attempt in range(MAX_REJOIN_ATTEMPTS):
                if guild_connected(ch):
                    break
                try:
                    await ch.connect(self_deaf=True)
                    rejoin_attempts = 0
                    log.info("Зашёл в голосовой канал %s", ch.name)
                    break
                except Exception as exc:
                    log.warning("Попытка входа %s не удалась: %s", attempt + 1, exc)
                    await asyncio.sleep(5)


@bot.event
async def on_voice_state_update(member: discord.Member, before, after) -> None:
    """Если бота выкинули из голоса не по /leave — вернуться в канал."""
    global rejoin_attempts
    if bot.user is None or member.id != bot.user.id:
        return
    if after.channel is not None:  # просто переместили — ок
        return
    if manual_leave or not want_voice:
        return

    await asyncio.sleep(3)
    guild = member.guild
    if isinstance(guild.voice_client, discord.VoiceClient) and guild.voice_client.is_connected():
        return
    if rejoin_attempts >= MAX_REJOIN_ATTEMPTS:
        return

    ch = _config_channel() or (
        before.channel
        if isinstance(before.channel, (discord.VoiceChannel, discord.StageChannel))
        else None
    )
    if ch is None:
        return

    try:
        await ch.connect(self_deaf=True)
        rejoin_attempts = 0
        log.info("Вернулся в голосовой канал %s", ch.name)
        if queue:
            ensure_player(guild)
    except Exception as exc:
        rejoin_attempts += 1
        log.warning("Не смог вернуться в канал: %s", exc)


# ------------------------- Команды -------------------------

@bot.tree.command(name="join", description="Зайти в голосовой канал")
async def cmd_join(interaction: discord.Interaction) -> None:
    global want_voice, manual_leave, rejoin_attempts
    ch = _voice_channel_for(interaction)
    if ch is None:
        return await reply(
            interaction,
            "❌ Зайди в голосовой канал или пропиши CHANNEL_ID в .env",
            ephemeral=True,
        )
    manual_leave = False
    want_voice = True
    rejoin_attempts = 0
    try:
        await _connect(ch)
    except Exception as exc:
        return await reply(interaction, f"❌ Не смог подключиться: {exc}", ephemeral=True)
    await reply(interaction, f"✅ Подключился к **{ch.name}**")


@bot.tree.command(name="leave", description="Выйти из голосового канала")
async def cmd_leave(interaction: discord.Interaction) -> None:
    global want_voice, manual_leave, current
    want_voice = False
    manual_leave = True
    queue.clear()
    current = None
    vc = interaction.guild.voice_client if interaction.guild else None
    if isinstance(vc, discord.VoiceClient):
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await vc.disconnect()
    await reply(interaction, "👋 Вышел из голосового канала")


@bot.tree.command(name="play", description="Играть трек или лоу-фай радио")
@app_commands.describe(query="Название, ссылка или, например: lofi hip hop radio")
async def cmd_play(interaction: discord.Interaction, query: str) -> None:
    global want_voice, manual_leave, rejoin_attempts
    ch = _voice_channel_for(interaction)
    if ch is None:
        return await reply(
            interaction,
            "❌ Зайди в голосовой канал или пропиши CHANNEL_ID в .env",
            ephemeral=True,
        )

    await interaction.response.send_message("🔍 Ищу…")

    manual_leave = False
    want_voice = True
    try:
        await _connect(ch)
    except Exception as exc:
        return await interaction.edit_original_response(
            content=f"❌ Не смог подключиться: {exc}"
        )

    try:
        track = await asyncio.wait_for(asyncio.to_thread(_extract, query), timeout=45)
    except asyncio.TimeoutError:
        return await interaction.edit_original_response(content="❌ Поиск затянулся")
    except Exception as exc:
        return await interaction.edit_original_response(
            content=f"❌ Не получилось получить трек: {exc}"
        )

    if len(queue) >= MAX_QUEUE:
        return await interaction.edit_original_response(
            content=f"⚠️ Очередь переполнена ({MAX_QUEUE})"
        )

    rejoin_attempts = 0
    queue.append(track)
    ensure_player(interaction.guild)

    live = " 📡" if track["is_live"] else ""
    await interaction.edit_original_response(
        content=f"➕ В очередь ({len(queue)}): **{track['title']}**{live}"
    )


@bot.tree.command(name="skip", description="Пропустить текущий трек")
async def cmd_skip(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client if interaction.guild else None
    if isinstance(vc, discord.VoiceClient) and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await reply(interaction, "⏭ Пропущен")
    else:
        await reply(interaction, "🎵 Сейчас ничего не играет", ephemeral=True)


@bot.tree.command(name="stop", description="Остановить и очистить очередь")
async def cmd_stop(interaction: discord.Interaction) -> None:
    global current
    queue.clear()
    current = None
    vc = interaction.guild.voice_client if interaction.guild else None
    if isinstance(vc, discord.VoiceClient) and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    await reply(interaction, "⏹ Остановлено, очередь очищена")


@bot.tree.command(name="pause", description="Пауза")
async def cmd_pause(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client if interaction.guild else None
    if isinstance(vc, discord.VoiceClient) and vc.is_playing():
        vc.pause()
        await reply(interaction, "⏸ Пауза")
    else:
        await reply(interaction, "🎵 Сейчас ничего не играет", ephemeral=True)


@bot.tree.command(name="resume", description="Продолжить воспроизведение")
async def cmd_resume(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client if interaction.guild else None
    if isinstance(vc, discord.VoiceClient) and vc.is_paused():
        vc.resume()
        await reply(interaction, "▶️ Продолжаю")
    else:
        await reply(interaction, "⚠️ Ничего не на паузе", ephemeral=True)


@bot.tree.command(name="queue", description="Показать текущую очередь")
async def cmd_queue(interaction: discord.Interaction) -> None:
    if current is None and not queue:
        return await reply(interaction, "🎵 Сейчас ничего не играет")
    lines = []
    if current:
        lines.append(f"▶️ **{current['title']}**")
    for i, t in enumerate(list(queue)[:10], 1):
        lines.append(f"{i}. {t['title']}")
    left = len(queue) - 10
    if left > 0:
        lines.append(f"… и ещё {left}")
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
