import discord
from discord.commands import SlashCommandGroup
from discord.ext import commands
import os
import json
import re
import asyncio
import base64
import io
import aiohttp
from dotenv import load_dotenv
from typing import Dict, Optional

# --- 定数定義 ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TIKTOK_API_URL = "https://tiktok-tts.weilnet.workers.dev/api/generation"
VOICE_ID = "jp_male_hikakin"  # HIKAKINボイスに固定

DATA_DIR = "data"
DICT_FILE = f"{DATA_DIR}/dictionaries.json"

# 絵文字
EMOJI_SUCCESS = "✅"
EMOJI_ERROR = "❌"
EMOJI_INFO = "ℹ️"
EMOJI_VC = "🔊"
EMOJI_TTS = "💬"
EMOJI_DICT = "📖"
EMOJI_HELP = "🤖"
EMOJI_WAVE = "👋"
EMOJI_QUEUE = "🎵"
EMOJI_MUTE = "🔇"
EMOJI_UNMUTE = "🔈"
EMOJI_PAUSE = "⏸️"
EMOJI_RESUME = "▶️"


# --- デバッグログ用ヘルパー ---
def log_debug(guild_id: Optional[int], message: str):
    """コンソールにデバッグログを出力する"""
    guild_tag = f"[{guild_id or 'GLOBAL'}]"
    print(f"[DEBUG] {guild_tag} {message}")


# --- データクラス (セッション管理) ---
class GuildSession:
    """サーバーごとのセッション情報を管理するクラス"""

    def __init__(self, bot_loop: asyncio.AbstractEventLoop, guild_id: int):
        self.guild_id = guild_id
        self.voice_client: Optional[discord.VoiceClient] = None
        self.text_channel_id: Optional[int] = None
        self.queue = asyncio.Queue()
        self.is_muted: bool = False
        log_debug(guild_id, "新しい再生タスクを作成します...")
        self.player_task = bot_loop.create_task(audio_player_task(guild_id))

    def stop(self):
        """セッションを停止し、関連タスクをクリーンアップする"""
        log_debug(self.guild_id, "GuildSession.stop() が呼び出されました。")
        if self.player_task and not self.player_task.done():
            self.player_task.cancel()
            log_debug(self.guild_id, "再生タスクをキャンセルしました。")
        self.voice_client = None


# --- グローバル変数 ---
guild_sessions: Dict[int, GuildSession] = {}
dictionaries: Dict[str, Dict[str, str]] = {}


# --- ヘルパー関数 ---
def load_data(filepath: str) -> dict:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_data(filepath: str, data: dict):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def create_embed(
    title: str, description: str, color: discord.Color = discord.Color.blue()
) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


# --- Botクラスの拡張 ---
class HikakinBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http_session: Optional[aiohttp.ClientSession] = None

    # ★★★ setup_hookを削除 ★★★

    async def on_close(self):
        """Bot終了時のクリーンアップ"""
        if self.http_session:
            await self.http_session.close()


# --- Botの初期化 ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = HikakinBot(intents=intents)


# --- 音声合成と再生タスク ---
async def generate_tts_bytes(text: str) -> Optional[bytes]:
    """TikTok TTS APIを呼び出し、音声データのバイト列を返す"""
    if not bot.http_session:
        log_debug(None, "HTTPセッションが初期化されていません。")
        return None

    headers = {"Content-Type": "application/json"}
    data = {"text": text, "voice": VOICE_ID}
    try:
        async with bot.http_session.post(
            TIKTOK_API_URL, json=data, headers=headers, timeout=10
        ) as response:
            if response.status == 200:
                json_data = await response.json()
                if json_data.get("success"):
                    audio_base64 = json_data["data"]
                    return base64.b64decode(audio_base64)
                else:
                    log_debug(None, f"TTS API Error: {json_data.get('error')}")
                    return None
            else:
                log_debug(
                    None,
                    f"TTS API HTTP Error: {response.status} - {await response.text()}",
                )
                return None
    except Exception as e:
        log_debug(None, f"TTS APIへのアクセス中にエラーが発生しました: {e}")
        return None


async def audio_player_task(guild_id: int):
    """各サーバーで独立して動作する音声再生バックグラウンドタスク"""
    log_debug(guild_id, "音声再生タスクが開始されました。")
    while True:
        try:
            session = guild_sessions.get(guild_id)
            if not session:
                log_debug(
                    guild_id, "セッションが見つからないため、再生タスクを停止します。"
                )
                break

            text = await session.queue.get()
            log_debug(guild_id, f"キューからアイテムを取得: '{text[:30]}...'")

            if not session.voice_client or not session.voice_client.is_connected():
                log_debug(guild_id, "VCに接続されていないため、再生をスキップします。")
                continue

            log_debug(guild_id, "音声合成を開始します...")
            audio_data = await generate_tts_bytes(text)
            if not audio_data:
                log_debug(guild_id, "音声合成に失敗しました。")
                continue
            log_debug(guild_id, "音声合成に成功しました。")

            source = discord.FFmpegPCMAudio(io.BytesIO(audio_data), pipe=True)

            log_debug(guild_id, "音声を再生します...")
            session.voice_client.play(source)

            while session.voice_client.is_playing() or session.voice_client.is_paused():
                await asyncio.sleep(0.5)
            log_debug(guild_id, "音声の再生が完了しました。")

        except asyncio.CancelledError:
            log_debug(guild_id, "再生タスクがキャンセルされました。")
            break
        except Exception as e:
            log_debug(guild_id, f"再生タスクで予期せぬエラーが発生しました: {e}")
            await asyncio.sleep(5)  # タスクがクラッシュしないように待機


def process_text_for_speech(
    message: discord.Message, dictionary: dict
) -> Optional[str]:
    """メッセージを読み上げ用に加工する"""
    text_to_read = message.clean_content
    for word, reading in dictionary.items():
        text_to_read = text_to_read.replace(word, reading)
    text_to_read = re.sub(r"https?://\S+", "URL", text_to_read)
    if message.attachments:
        text_to_read += " 添付ファイル"
    return text_to_read.strip() or None


# --- Botイベント ---
@bot.event
async def on_ready():
    global dictionaries
    # ★★★ ここでHTTPセッションを初期化 ★★★
    if not bot.http_session or bot.http_session.closed:
        bot.http_session = aiohttp.ClientSession()
        log_debug(None, "HTTPセッションを初期化しました。")

    dictionaries = load_data(DICT_FILE)
    log_debug(None, f"{bot.user} としてログインしました。")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    
    guild_id = message.guild.id
    session = guild_sessions.get(guild_id)

    if not session or session.text_channel_id != message.channel.id or session.is_muted:
        return

    # sessionに保存されたVCを信用せず、guildから現在の最新のVCを取得する
    active_vc = message.guild.voice_client

    # 最新のVCが存在しない場合、本当の意味で接続が切れている
    if not active_vc:
        log_debug(guild_id, "GuildがアクティブなVCを報告していません。メッセージを無視します。")
        return

    # 念のため、セッションが保持するVC情報を最新のものに更新しておく
    session.voice_client = active_vc

    if message.content.lower() == "s":
        log_debug(guild_id, "スキップコマンド 's' を受信しました。")
        can_skip = session.voice_client.is_playing() or not session.queue.empty()
        if can_skip:
            log_debug(guild_id, "スキップ処理中... キューをクリアし、再生を停止します。")
            while not session.queue.empty():
                try:
                    session.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            if session.voice_client.is_playing():
                session.voice_client.stop()
            await message.add_reaction("⏩")
        else:
            log_debug(guild_id, "スキップ対象がありません。")
            await message.add_reaction("❌")
        return

    dictionary = dictionaries.get(str(guild_id), {})
    text_to_speak = process_text_for_speech(message, dictionary)
    
    if text_to_speak:
        log_debug(guild_id, f"キューに追加: '{text_to_speak[:30]}...'")
        await session.queue.put(text_to_speak)

@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
):
    if member.id == bot.user.id:
        return

    guild_id = member.guild.id
    session = guild_sessions.get(guild_id)
    if not session or not session.voice_client:
        return

    vc_channel = session.voice_client.channel
    text = None
    if before.channel != vc_channel and after.channel == vc_channel:
        text = f"{member.display_name}さんが参加しました"
    elif before.channel == vc_channel and after.channel != vc_channel:
        text = f"{member.display_name}さんが退出しました"

    if text:
        log_debug(guild_id, f"入退室通知をキューに追加: '{text}'")
        await session.queue.put(text)


# --- スラッシュコマンドグループ ---
vc_group = SlashCommandGroup("vc", "ボイスチャンネル関連の操作")
dict_group = SlashCommandGroup("dict", "読み上げ辞書関連のコマンド")


# --- VC関連コマンド ---
@vc_group.command(
    name="join", description="VCに参加し、このチャンネルの読み上げを開始します。"
)
async def vc_join(ctx: discord.ApplicationContext):
    guild_id = ctx.guild.id
    log_debug(guild_id, f"/vc join が {ctx.author} によって実行されました。")

    if not ctx.author.voice:
        return await ctx.respond(
            embed=create_embed(
                f"{EMOJI_ERROR} エラー",
                "先にボイスチャンネルに参加してください。",
                discord.Color.red(),
            ),
            ephemeral=True,
        )

    try:
        # 最初に暫定的なメッセージで即時応答する
        await ctx.respond(
            f"{EMOJI_VC} **{ctx.author.voice.channel.name}** への接続を開始します..."
        )

        voice_channel = ctx.author.voice.channel

        if guild_id in guild_sessions:
            log_debug(guild_id, "既存のセッションを停止・削除します。")
            guild_sessions[guild_id].stop()
            del guild_sessions[guild_id]

        if ctx.guild.voice_client:
            await ctx.guild.voice_client.disconnect(force=True)

        log_debug(guild_id, f"VC '{voice_channel.name}' に接続します...")
        vc = await voice_channel.connect()
        await ctx.guild.change_voice_state(channel=voice_channel, self_deaf=True)
        log_debug(guild_id, "接続とスピーカーミュートに成功しました。")

        log_debug(guild_id, "新しいGuildSessionを作成します。")
        session = GuildSession(bot.loop, guild_id)
        session.voice_client = vc
        session.text_channel_id = ctx.channel.id
        guild_sessions[guild_id] = session
        log_debug(guild_id, "新しいセッションの作成に成功しました。")

        embed = create_embed(
            f"{EMOJI_VC} 接続しました", f"**{voice_channel.name}** に参加しました。"
        )
        embed.add_field(name="読み上げチャンネル", value=ctx.channel.mention)

        # 最初のメッセージを編集して最終的な結果を表示する
        await ctx.edit(content=None, embed=embed)

    except Exception as e:
        log_debug(guild_id, f"VCへの参加処理中にエラーが発生しました: {e}")
        error_embed = create_embed(
            f"{EMOJI_ERROR} エラー", f"接続に失敗しました: {e}", discord.Color.red()
        )
        # エラーが発生した場合も、最初のメッセージを編集するか、新しいメッセージを送る
        if not ctx.interaction.response.is_done():
            await ctx.respond(embed=error_embed, ephemeral=True)
        else:
            await ctx.edit(content=None, embed=error_embed)


@vc_group.command(name="leave", description="VCから退出します。")
async def vc_leave(ctx: discord.ApplicationContext):
    try:
        await ctx.respond(f"{EMOJI_WAVE} 退出処理を開始します...")

        guild_id = ctx.guild.id
        log_debug(guild_id, f"/vc leave が {ctx.author} によって実行されました。")

        if not ctx.guild.voice_client:
            return await ctx.edit(
                content=None,
                embed=create_embed(
                    f"{EMOJI_ERROR} エラー",
                    "BotはVCに参加していません。",
                    discord.Color.red(),
                ),
            )

        if guild_id in guild_sessions:
            log_debug(guild_id, "再生タスクを停止し、セッションを削除します。")
            guild_sessions[guild_id].stop()
            del guild_sessions[guild_id]

        await ctx.guild.voice_client.disconnect()
        log_debug(guild_id, "VCから切断しました。")

        await ctx.edit(
            content=None,
            embed=create_embed(
                f"{EMOJI_WAVE} 退出しました", "ボイスチャンネルから退出しました。"
            ),
        )

    except Exception as e:
        log_debug(guild_id, f"VCからの退出処理中にエラーが発生しました: {e}")
        error_embed = create_embed(
            f"{EMOJI_ERROR} エラー", f"退出に失敗しました: {e}", discord.Color.red()
        )
        if not ctx.interaction.response.is_done():
            await ctx.respond(embed=error_embed, ephemeral=True)
        else:
            await ctx.edit(content=None, embed=error_embed)


@vc_group.command(name="mute", description="読み上げを一時的に停止します。")
async def vc_mute(ctx: discord.ApplicationContext):
    session = guild_sessions.get(ctx.guild.id)
    if session:
        session.is_muted = True
        await ctx.respond(
            embed=create_embed(
                f"{EMOJI_MUTE} ミュートしました",
                "メッセージの読み上げを停止します。\n`/vc unmute` で再開できます。",
            )
        )


@vc_group.command(name="unmute", description="読み上げのミュートを解除します。")
async def vc_unmute(ctx: discord.ApplicationContext):
    session = guild_sessions.get(ctx.guild.id)
    if session:
        session.is_muted = False
        await ctx.respond(
            embed=create_embed(
                f"{EMOJI_UNMUTE} ミュート解除", "メッセージの読み上げを再開します。"
            )
        )


# --- 辞書関連コマンド ---
@dict_group.command(name="add", description="辞書に単語と読みを登録します。")
async def dict_add(ctx: discord.ApplicationContext, word: str, reading: str):
    guild_id = str(ctx.guild.id)
    if guild_id not in dictionaries:
        dictionaries[guild_id] = {}
    dictionaries[guild_id][word] = reading
    save_data(DICT_FILE, dictionaries)
    await ctx.respond(
        embed=create_embed(
            f"{EMOJI_SUCCESS} 辞書登録",
            f"「**{word}**」を「**{reading}**」として登録しました。",
        ),
        ephemeral=True,
    )


@dict_group.command(name="remove", description="辞書から単語を削除します。")
async def dict_remove(ctx: discord.ApplicationContext, word: str):
    guild_id = str(ctx.guild.id)
    if guild_id in dictionaries and word in dictionaries[guild_id]:
        del dictionaries[guild_id][word]
        save_data(DICT_FILE, dictionaries)
        await ctx.respond(
            embed=create_embed(
                f"{EMOJI_SUCCESS} 辞書削除", f"「**{word}**」を削除しました。"
            ),
            ephemeral=True,
        )
    else:
        await ctx.respond(
            embed=create_embed(
                f"{EMOJI_ERROR} エラー",
                f"「**{word}**」は見つかりませんでした。",
                discord.Color.red(),
            ),
            ephemeral=True,
        )


@dict_group.command(name="list", description="登録されている単語の一覧を表示します。")
async def dict_list(ctx: discord.ApplicationContext):
    guild_id = str(ctx.guild.id)
    dictionary = dictionaries.get(guild_id, {})
    if not dictionary:
        return await ctx.respond(
            embed=create_embed(f"{EMOJI_DICT} 辞書一覧", "辞書は空です。"),
            ephemeral=True,
        )

    embed = create_embed(
        f"{EMOJI_DICT} {ctx.guild.name} の辞書一覧",
        "\n".join([f"・`{w}` → `{r}`" for w, r in dictionary.items()]),
        discord.Color.green(),
    )
    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(name="help", description="Botのコマンド一覧と使い方を表示します。")
async def help_command(ctx: discord.ApplicationContext):
    embed = create_embed(
        f"{EMOJI_HELP} HIKAKIN読み上げBot ヘルプ",
        "TikTokのHIKAKINボイスでメッセージを読み上げるBotです。\n各コマンドの詳しい使い方を以下に示します。",
    )

    vc_description = (
        "`/vc join`: あなたがいるVCに参加し、このチャンネルの読み上げを開始します。\n"
        "`/vc leave`: VCから退出します。\n"
        "`/vc mute`: メッセージの読み上げを一時的に停止します。\n"
        "`/vc unmute`: 読み上げを再開します。"
    )
    embed.add_field(
        name=f"{EMOJI_VC} VC関連コマンド", value=vc_description, inline=False
    )

    dict_description = (
        "`/dict add [word] [reading]`: 単語とその読みを辞書に登録します。\n"
        "`/dict remove [word]`: 辞書から単語を削除します。\n"
        "`/dict list`: 登録されている単語の一覧を表示します。"
    )
    embed.add_field(
        name=f"{EMOJI_DICT} 辞書関連コマンド", value=dict_description, inline=False
    )

    other_description = (
        "**VCへの参加/退出通知**: ユーザーがVCに出入りすると、その旨を読み上げます。"
    )
    embed.add_field(name="✨ その他の機能", value=other_description, inline=False)

    await ctx.respond(embed=embed, ephemeral=True)


# --- コマンド登録 ---
bot.add_application_command(vc_group)
bot.add_application_command(dict_group)

# --- Bot実行 ---
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("エラー: .envファイルにDISCORD_TOKENを設定してください。")
    else:
        bot.run(DISCORD_TOKEN)
