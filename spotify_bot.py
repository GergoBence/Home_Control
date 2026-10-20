import os
from dotenv import load_dotenv
import time
import threading
import unicodedata
import audioop
import logging

import discord
import pyautogui
import speech_recognition as sr
from discord.ext import commands, voice_recv

logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)
load_dotenv()

# ============================================================
# BEÁLLÍTÁSOK
# ============================================================

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  

SILENCE_THRESHOLD = 500
SILENCE_DURATION = 0.8
MIN_SPEECH_DURATION = 0.3

BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

# A hangpuffer állapota felhasználónként
audio_buffers = {}
audio_lock = threading.Lock()

recognizer = sr.Recognizer()


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

client = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# SEGÉDFÜGGVÉNYEK
# ============================================================

def normalize_text(text):
    """Kisbetűs, ékezet nélküli szöveg."""
    text = text.lower()

    normalized = unicodedata.normalize("NFD", text)

    return "".join(
        char
        for char in normalized
        if unicodedata.category(char) != "Mn"
    )


def press_media_key(key, presses=1):
    """Windows médialejátszó vezérlése."""
    try:
        for _ in range(presses):
            pyautogui.press(key)
        print(f"Médiavezérlés: {key} (ismétlés: {presses})")

    except Exception as error:
        print(f"Médiavezérlési hiba: {error}")


# ============================================================
# FELISMERT PARANCSOK KEZELÉSE
# ============================================================

def handle_recognized_text(text):
    normalized = normalize_text(text)

    print(f"Felismert parancs: {normalized}")

    # Következő szám
    if any(word in normalized for word in (
        "kovetkezo",
        "kovetkezo szam",
        "next",
        "next track"
    )):
        press_media_key("nexttrack")
        return

    # Előző szám
    if any(word in normalized for word in (
        "elozo",
        "vissza",
        "previous",
        "prev"
    )):
        press_media_key("prevtrack")
        return

    # Lejátszás / szünet
    if any(word in normalized for word in (
        "start",
        "stop",
        "szunet",
        "lejatszas",
        "play",
        "pause",
        "inditsd el",
        "allitsd meg"
    )):
        press_media_key("playpause")
        return

    # Hangerő növelése
    if any(word in normalized for word in (
        "hangos",
        "hang fel",
        "hangosabb",
        "hangosabban",
        "hangero fel",
        "noveld a hangot",
        "vol up",
        "volume up"
    )):
        press_media_key("volumeup", presses=5)
        return

    # Hangerő csökkentése
    if any(word in normalized for word in (
        "halk",
        "hang le",
        "halkabb",
        "halkabban",
        "hangero le",
        "vedd le a hangot",
        "vol down",
        "volume down"
    )):
        press_media_key("volumedown", presses=5)
        return

    print(f"Nem ismert parancs: {text}")


# ============================================================
# BESZÉDFELISMERÉS
# ============================================================

def transcribe_pcm(pcm_data):
    """A sztereó PCM-hangot monóvá alakítja, majd felismeri."""

    if not pcm_data:
        return None

    # A Google felismeréshez monó hangot használunk.
    mono_pcm = audioop.tomono(
        pcm_data,
        SAMPLE_WIDTH,
        0.5,
        0.5
    )

    audio_data = sr.AudioData(
        mono_pcm,
        SAMPLE_RATE,
        SAMPLE_WIDTH
    )

    try:
        return recognizer.recognize_google(
            audio_data,
            language="hu-HU"
        )

    except sr.UnknownValueError:
        print("A beszéd nem volt felismerhető.")
        return None

    except sr.RequestError as error:
        print(f"Google beszédfelismerési hiba: {error}")
        return None


def _process_speech_worker(user_id, pcm_data):
    """A hálózati beszédfelismerés külön szálon fut."""

    try:
        text = transcribe_pcm(pcm_data)

        if text:
            print(f"Felismert hang ({user_id}): {text}")
            handle_recognized_text(text)

    except Exception as error:
        print(f"Beszédfeldolgozási hiba: {error}")


def process_speech(user_id, pcm_data):
    """
    Szinkron függvény: új szálat indít.
    Nem coroutine, ezért nem szabad
    asyncio.run_coroutine_threadsafe()-szel hívni.
    """

    threading.Thread(
        target=_process_speech_worker,
        args=(user_id, pcm_data),
        daemon=True
    ).start()


# ============================================================
# DISCORD HANGFOGADÁS
# ============================================================

def on_audio(user, data):
    """
    A BasicSink hívja meg.
    A callback nem async függvény.
    """

    if user is None:
        return

    if getattr(user, "bot", False):
        return

    pcm = data.pcm

    if not pcm:
        return

    try:
        volume = audioop.rms(pcm, SAMPLE_WIDTH)
    except Exception as error:
        print(f"Hangerőmérési hiba: {error}")
        return

    now = time.monotonic()
    user_id = user.id

    with audio_lock:
        state = audio_buffers.get(user_id)

        if state is None:
            state = {
                "buffer": bytearray(),
                "speaking": False,
                "last_voice": 0.0
            }
            audio_buffers[user_id] = state

        if volume >= SILENCE_THRESHOLD:
            if not state["speaking"]:
                state["buffer"].clear()
                state["speaking"] = True

            state["buffer"].extend(pcm)
            state["last_voice"] = now

        elif state["speaking"]:
            # A beszéd végén a rövid csendet is megtartjuk.
            state["buffer"].extend(pcm)


# ============================================================
# CSEND FIGYELÉSE
# ============================================================

def audio_finalizer():
    """
    Háttérszál, amely akkor is lezárja a beszédet,
    ha a csend alatt már nem érkezik új hangcsomag.
    """

    while True:
        time.sleep(0.1)

        completed = []
        now = time.monotonic()

        with audio_lock:
            for user_id, state in audio_buffers.items():

                if not state["speaking"]:
                    continue

                silence_time = now - state["last_voice"]

                if silence_time >= SILENCE_DURATION:
                    captured = bytes(state["buffer"])

                    state["buffer"].clear()
                    state["speaking"] = False
                    state["last_voice"] = 0.0

                    if len(captured) >= (
                        BYTES_PER_SECOND * MIN_SPEECH_DURATION
                    ):
                        completed.append((user_id, captured))

        # A felismerést a záron kívül indítjuk.
        for user_id, captured in completed:
            process_speech(user_id, captured)


# A csendfigyelő elindítása
threading.Thread(
    target=audio_finalizer,
    daemon=True,
    name="audio-finalizer"
).start()


# ============================================================
# DISCORD PARANCSOK
# ============================================================

@client.command(name="join")
async def join_command(ctx):

    if ctx.author.voice is None:
        await ctx.send(
            "Előbb lépj be egy hangcsatornába!"
        )
        return

    channel = ctx.author.voice.channel
    voice_client = ctx.voice_client

    try:
        if voice_client is not None and voice_client.is_connected():

            if not isinstance(
                voice_client,
                voice_recv.VoiceRecvClient
            ):
                await voice_client.disconnect()
                voice_client = None

            else:
                if voice_client.is_listening():
                    voice_client.stop_listening()

                if voice_client.channel != channel:
                    await voice_client.move_to(channel)

        if voice_client is None:
            voice_client = await channel.connect(
                cls=voice_recv.VoiceRecvClient
            )

        voice_client.listen(
            voice_recv.BasicSink(on_audio)
        )

        await ctx.send(
            f"Csatlakoztam: **{channel.name}**. "
            "Mondhatsz hangparancsokat."
        )

        print(f"Hangfogadás elindítva: {channel.name}")

    except Exception as error:
        print(f"Csatlakozási hiba: {error}")
        await ctx.send(
            f"Nem sikerült elindítani a hangfogadást: {error}"
        )


@client.command(name="leave")
async def leave_command(ctx):

    voice_client = ctx.voice_client

    if voice_client is None:
        await ctx.send("Nem vagyok hangcsatornában.")
        return

    try:
        if isinstance(
            voice_client,
            voice_recv.VoiceRecvClient
        ):
            voice_client.stop_listening()

        await voice_client.disconnect()

        await ctx.send("Kiléptem a hangcsatornából.")
        print("Hangfogadás leállítva.")

    except Exception as error:
        print(f"Kilépési hiba: {error}")
        await ctx.send(f"Hiba kilépés közben: {error}")


@client.command(name="next")
async def next_command(ctx):
    press_media_key("nexttrack")
    await ctx.send("Következő szám.")


@client.command(name="prev")
async def prev_command(ctx):
    press_media_key("prevtrack")
    await ctx.send("Előző szám.")


@client.command(name="pause")
async def pause_command(ctx):
    press_media_key("playpause")
    await ctx.send("Lejátszás / szünet.")


@client.command(name="play")
async def play_command(ctx):
    press_media_key("playpause")
    await ctx.send("Lejátszás / szünet.")

@client.command(name="up")
async def volume_up_command(ctx):
    press_media_key("volumeup")
    await ctx.send("Hangerő növelve.")

@client.command(name="down")
async def volume_down_command(ctx):
    press_media_key("volumedown")
    await ctx.send("Hangerő csökkentve.")

# ============================================================
# ESEMÉNYEK
# ============================================================

@client.event
async def on_ready():
    print(f"Bejelentkezve: {client.user}")
    print("Parancsok: !join, !leave, !next, !prev, !pause, !play")


@client.event
async def on_command_error(ctx, error):

    if isinstance(error, commands.CommandNotFound):
        return

    print(f"Discord parancshiba: {error}")


# ============================================================
# INDÍTÁS
# ============================================================


TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("Hiányzik a DISCORD_TOKEN környezeti változó!")

client.run(TOKEN)
