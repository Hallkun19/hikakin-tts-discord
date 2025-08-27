# main.py

import discord
from discord.ext import commands, tasks
from discord import app_commands  # スラッシュコマンドのために必要

import os
import json
import re
import asyncio
import base64
import io
import aiohttp
from dotenv import load_dotenv
from typing import Dict, Optional, List

# --- .envファイルから環境変数を読み込み ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    print("エラー: .envファイルにDISCORD_TOKENを設定してください。")
    exit()

# --- 定数定義 ---
TIKTOK_API_URL = "https://tiktok-tts.weilnet.workers.dev/api/generation"
VOICE_ID = "jp_male_hikakin"
DATA_DIR = "data"
DICT_FILE = f"{DATA_DIR}/dictionaries.json"

# 絵文字定義
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
EMOJI_SKIP = "⏩"

# --- ヘルパー関数 ---


def log_debug(guild_id: Optional[int], message: str):
    """デバッグメッセージをコンソールに出力する"""
    guild_tag = f"[{guild_id or 'GLOBAL'}]"
    print(f"[DEBUG] {guild_tag} {message}")


def load_data(filepath: str) -> dict:
    """JSONファイルからデータを読み込む"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_data(filepath: str, data: dict):
    """データをJSONファイルに書き込む"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def create_embed(
    title: str, description: str, color: discord.Color = discord.Color.blue()
) -> discord.Embed:
    """定型的なEmbedオブジェクトを作成する"""
    return discord.Embed(title=title, description=description, color=color)


def process_text_for_speech(
    message: discord.Message, dictionary: dict
) -> Optional[str]:
    """読み上げ用にテキストを処理する"""
    text_to_read = message.clean_content
    # 辞書置換
    for word, reading in dictionary.items():
        text_to_read = text_to_read.replace(word, reading)

    # <>で囲まれた部分（絵文字、メンションなど）を削除
    text_to_read = re.sub(r"<.*?>", "", text_to_read)

    # URLを置換
    text_to_read = re.sub(r"https?://\S+", "URL", text_to_read)
    # 添付ファイル
    if message.attachments:
        text_to_read += " 添付ファイル"
    return text_to_read.strip() or None


# --- サーバーごとのセッション管理クラス ---


class GuildSession:
    """サーバーごとの読み上げセッションを管理するクラス"""

    def __init__(self, bot: "HikakinBot", guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.voice_client: Optional[discord.VoiceClient] = None
        self.text_channel_id: Optional[int] = None
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.is_muted: bool = False
        self._player_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def start(self, voice_client: discord.VoiceClient, text_channel_id: int):
        """セッションを開始し、再生タスクを起動する"""
        self.voice_client = voice_client
        self.text_channel_id = text_channel_id
        if self._player_task and not self._player_task.done():
            self._player_task.cancel()
        self._player_task = self.bot.loop.create_task(self._audio_player_task())
        log_debug(self.guild_id, "新しい再生タスクを作成・開始しました。")

    async def stop(self):
        """セッションを停止し、リソースを解放する"""
        log_debug(self.guild_id, "GuildSession.stop() が呼び出されました。")
        self._stop_event.set()  # タスクに停止を通知
        if self._player_task and not self._player_task.done():
            self._player_task.cancel()
            log_debug(self.guild_id, "再生タスクをキャンセルしました。")
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
        self.voice_client = None

    async def _generate_tts_bytes(self, text: str) -> Optional[bytes]:
        """TikTok TTS APIを叩いて音声データを取得する"""
        headers = {"Content-Type": "application/json"}
        data = {"text": text, "voice": VOICE_ID}
        try:
            async with self.bot.http_session.post(
                TIKTOK_API_URL, json=data, headers=headers, timeout=20
            ) as response:
                if response.status == 200:
                    json_data = await response.json()
                    if json_data.get("success"):
                        return base64.b64decode(json_data["data"])
                    else:
                        log_debug(
                            self.guild_id, f"TTS API Error: {json_data.get('error')}"
                        )
                else:
                    log_debug(self.guild_id, f"TTS API HTTP Error: {response.status}")
        except Exception as e:
            log_debug(
                self.guild_id, f"TTS APIへのアクセス中にエラーが発生しました: {e}"
            )
        return None

    async def _audio_player_task(self):
        """キューからテキストを取り出し、音声を再生し続けるタスク"""
        log_debug(self.guild_id, "音声再生タスクが開始されました。")
        while not self.bot.is_closed() and not self._stop_event.is_set():
            try:
                text = await self.queue.get()
                log_debug(self.guild_id, f"キューから取得: '{text[:30]}...'")

                if not self.voice_client or not self.voice_client.is_connected():
                    log_debug(self.guild_id, "VC未接続のため再生をスキップ。")
                    continue

                audio_data = await self._generate_tts_bytes(text)
                if not audio_data:
                    log_debug(self.guild_id, "音声合成に失敗しました。")
                    continue

                source = discord.FFmpegPCMAudio(io.BytesIO(audio_data), pipe=True)
                log_debug(self.guild_id, "再生を開始します...")
                self.voice_client.play(source)

                # 再生完了を待つ
                while self.voice_client.is_playing() or self.voice_client.is_paused():
                    await asyncio.sleep(0.5)
                log_debug(self.guild_id, "再生が完了しました。")

            except asyncio.CancelledError:
                log_debug(self.guild_id, "再生タスクがキャンセルされました。")
                break
            except Exception as e:
                log_debug(self.guild_id, f"再生タスクで予期せぬエラー: {e}")
                await asyncio.sleep(5)
        log_debug(self.guild_id, "音声再生タスクが終了しました。")


# --- Cog: VC関連コマンド ---


class VoiceCog(commands.Cog, name="VC関連"):
    """ボイスチャンネルへの参加、退出、ミュートなどのコマンドを管理"""

    def __init__(self, bot: "HikakinBot"):
        self.bot = bot

    @app_commands.command(
        name="join", description="VCに参加し、このチャンネルの読み上げを開始します。"
    )
    async def join(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        log_debug(guild_id, f"/join が {interaction.user} によって実行されました。")

        if not interaction.user.voice or not interaction.user.voice.channel:
            embed = create_embed(
                f"{EMOJI_ERROR} エラー",
                "先にボイスチャンネルに参加してください。",
                discord.Color.red(),
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        await interaction.response.defer()  # 処理に時間がかかることを通知

        try:
            # 既存のセッションがあれば停止・削除
            if guild_id in self.bot.guild_sessions:
                log_debug(guild_id, "既存のセッションを停止します。")
                await self.bot.guild_sessions[guild_id].stop()
                del self.bot.guild_sessions[guild_id]

            voice_channel = interaction.user.voice.channel
            log_debug(guild_id, f"VC '{voice_channel.name}' に接続します...")
            vc = await voice_channel.connect()
            await interaction.guild.change_voice_state(
                channel=voice_channel, self_deaf=True
            )
            log_debug(guild_id, "接続とスピーカーミュートに成功しました。")

            # 新しいセッションを作成して開始
            session = GuildSession(self.bot, guild_id)
            session.start(vc, interaction.channel_id)
            self.bot.guild_sessions[guild_id] = session
            log_debug(guild_id, "新しいセッションの作成に成功しました。")

            embed = create_embed(
                f"{EMOJI_VC} 接続しました", f"**{voice_channel.name}** に参加しました。"
            )
            embed.add_field(
                name="読み上げチャンネル", value=interaction.channel.mention
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            log_debug(guild_id, f"VC参加処理中にエラー: {e}")
            embed = create_embed(
                f"{EMOJI_ERROR} エラー", f"接続に失敗しました: {e}", discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="leave", description="VCから退出します。")
    async def leave(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        log_debug(guild_id, f"/leave が {interaction.user} によって実行されました。")
        await interaction.response.defer()

        if guild_id not in self.bot.guild_sessions:
            embed = create_embed(
                f"{EMOJI_ERROR} エラー",
                "BotはVCに参加していません。",
                discord.Color.red(),
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        try:
            log_debug(guild_id, "セッションを停止し、削除します。")
            await self.bot.guild_sessions[guild_id].stop()
            del self.bot.guild_sessions[guild_id]

            embed = create_embed(
                f"{EMOJI_WAVE} 退出しました", "ボイスチャンネルから退出しました。"
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            log_debug(guild_id, f"VC退出処理中にエラー: {e}")
            embed = create_embed(
                f"{EMOJI_ERROR} エラー", f"退出に失敗しました: {e}", discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="mute", description="読み上げを一時的に停止します。")
    async def mute(self, interaction: discord.Interaction):
        session = self.bot.guild_sessions.get(interaction.guild_id)
        if session:
            session.is_muted = True
            embed = create_embed(
                f"{EMOJI_MUTE} ミュートしました",
                "メッセージの読み上げを停止します。\n`/unmute` で再開できます。",
            )
            await interaction.response.send_message(embed=embed)
        else:
            embed = create_embed(
                f"{EMOJI_ERROR} エラー",
                "BotはVCに参加していません。",
                discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="unmute", description="読み上げのミュートを解除します。")
    async def unmute(self, interaction: discord.Interaction):
        session = self.bot.guild_sessions.get(interaction.guild_id)
        if session:
            session.is_muted = False
            embed = create_embed(
                f"{EMOJI_UNMUTE} ミュート解除", "メッセージの読み上げを再開します。"
            )
            await interaction.response.send_message(embed=embed)
        else:
            embed = create_embed(
                f"{EMOJI_ERROR} エラー",
                "BotはVCに参加していません。",
                discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


# --- Cog: 辞書関連コマンド ---


@app_commands.guild_only()
class DictionaryCog(commands.Cog, name="辞書関連"):
    """読み上げ辞書の登録、削除、一覧表示コマンドを管理"""

    def __init__(self, bot: "HikakinBot"):
        self.bot = bot

    # グループコマンドを定義
    dict_group = app_commands.Group(
        name="dictionary", description="読み上げ辞書関連のコマンド"
    )

    @dict_group.command(name="add", description="辞書に単語と読みを登録します。")
    async def add(self, interaction: discord.Interaction, word: str, reading: str):
        guild_id = str(interaction.guild_id)
        if guild_id not in self.bot.dictionaries:
            self.bot.dictionaries[guild_id] = {}
        self.bot.dictionaries[guild_id][word] = reading
        save_data(DICT_FILE, self.bot.dictionaries)
        embed = create_embed(
            f"{EMOJI_SUCCESS} 辞書登録",
            f"「**{word}**」を「**{reading}**」として登録しました。",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @dict_group.command(name="remove", description="辞書から単語を削除します。")
    async def remove(self, interaction: discord.Interaction, word: str):
        guild_id = str(interaction.guild_id)
        if (
            guild_id in self.bot.dictionaries
            and word in self.bot.dictionaries[guild_id]
        ):
            del self.bot.dictionaries[guild_id][word]
            save_data(DICT_FILE, self.bot.dictionaries)
            embed = create_embed(
                f"{EMOJI_SUCCESS} 辞書削除", f"「**{word}**」を削除しました。"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            embed = create_embed(
                f"{EMOJI_ERROR} エラー",
                f"「**{word}**」は見つかりませんでした。",
                discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @dict_group.command(
        name="list", description="登録されている単語の一覧を表示します。"
    )
    async def list(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        dictionary = self.bot.dictionaries.get(guild_id, {})
        if not dictionary:
            embed = create_embed(f"{EMOJI_DICT} 辞書一覧", "辞書は空です。")
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        description = "\n".join([f"・`{w}` → `{r}`" for w, r in dictionary.items()])
        embed = create_embed(
            f"{EMOJI_DICT} {interaction.guild.name} の辞書一覧",
            description,
            discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# --- Cog: ヘルプコマンド ---


class HelpCog(commands.Cog, name="ヘルプ"):
    def __init__(self, bot: "HikakinBot"):
        self.bot = bot

    @app_commands.command(
        name="help", description="Botのコマンド一覧と使い方を表示します。"
    )
    async def help(self, interaction: discord.Interaction):
        embed = create_embed(
            f"{EMOJI_HELP} HIKAKIN読み上げBot ヘルプ",
            "TikTokのHIKAKINボイスでメッセージを読み上げるBotです。\n各コマンドの詳しい使い方を以下に示します。",
        )
        vc_description = (
            "`/join`: あなたがいるVCに参加し、このチャンネルの読み上げを開始します。\n"
            "`/leave`: VCから退出します。\n"
            "`/mute`: メッセージの読み上げを一時的に停止します。\n"
            "`/unmute`: 読み上げを再開します。"
        )
        embed.add_field(
            name=f"{EMOJI_VC} VC関連コマンド", value=vc_description, inline=False
        )

        dict_description = (
            "`/dictionary add [word] [reading]`: 単語とその読みを辞書に登録します。\n"
            "`/dictionary remove [word]`: 辞書から単語を削除します。\n"
            "`/dictionary list`: 登録されている単語の一覧を表示します。"
        )
        embed.add_field(
            name=f"{EMOJI_DICT} 辞書関連コマンド", value=dict_description, inline=False
        )

        other_description = (
            "**VCへの参加/退出通知**: ユーザーがVCに出入りすると、その旨を読み上げます。\n"
            f"**読み上げスキップ**: 読み上げ中にテキストチャンネルで `s` と送信すると、現在の読み上げを停止し、キューをクリアします。"
        )
        embed.add_field(name="✨ その他の機能", value=other_description, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# --- メインのBotクラス ---


class HikakinBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.guild_sessions: Dict[int, GuildSession] = {}
        self.dictionaries: Dict[str, Dict[str, str]] = {}

    async def setup_hook(self):
        """Bot起動時に実行される初期化処理"""
        log_debug(None, "setup_hookを開始します...")
        self.http_session = aiohttp.ClientSession()
        self.dictionaries = load_data(DICT_FILE)

        # Cog（機能別クラス）を登録
        await self.add_cog(VoiceCog(self))
        await self.add_cog(DictionaryCog(self))
        await self.add_cog(HelpCog(self))

        # スラッシュコマンドをDiscordに登録（同期）
        # 特定のサーバーでのみテストする場合は guild=discord.Object(id=YOUR_GUILD_ID) を追加
        await self.tree.sync()
        log_debug(None, "セットアップが完了しました。")

    async def on_ready(self):
        log_debug(None, f"{self.user} としてログインしました。")
        log_debug(None, "Botの準備が完了しました。")

    async def on_close(self):
        """Bot終了時に実行されるクリーンアップ処理"""
        log_debug(None, "Botを終了します...")
        for session in self.guild_sessions.values():
            await session.stop()
        if self.http_session:
            await self.http_session.close()
        log_debug(None, "クリーンアップが完了しました。")

    async def on_message(self, message: discord.Message):
        """メッセージ受信時のイベントハンドラ"""
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        session = self.guild_sessions.get(guild_id)

        if (
            not session
            or session.text_channel_id != message.channel.id
            or session.is_muted
        ):
            return

        # スキップコマンド ('s')
        if message.content.lower() == "s":
            log_debug(guild_id, "スキップコマンド 's' を受信しました。")
            can_skip = (
                session.voice_client and session.voice_client.is_playing()
            ) or not session.queue.empty()
            if can_skip:
                log_debug(
                    guild_id, "スキップ処理中... キューをクリアし、再生を停止します。"
                )
                # キューを空にする
                while not session.queue.empty():
                    try:
                        session.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                # 再生中なら停止
                if session.voice_client and session.voice_client.is_playing():
                    session.voice_client.stop()
                await message.add_reaction(EMOJI_SKIP)
            else:
                log_debug(guild_id, "スキップ対象がありません。")
                await message.add_reaction(EMOJI_ERROR)
            return

        dictionary = self.dictionaries.get(str(guild_id), {})
        text_to_speak = process_text_for_speech(message, dictionary)
        if text_to_speak:
            log_debug(guild_id, f"キューに追加: '{text_to_speak[:30]}...'")
            await session.queue.put(text_to_speak)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        """ボイスチャンネルの状態変化時のイベントハンドラ"""
        if member.bot:
            return

        guild_id = member.guild.id
        session = self.guild_sessions.get(guild_id)

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


# --- Botの起動 ---


def main():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True

    bot = HikakinBot(command_prefix="!", intents=intents)

    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("エラー: 不正なDiscordトークンです。 .env ファイルを確認してください。")
    except Exception as e:
        print(f"Botの実行中に予期せぬエラーが発生しました: {e}")


if __name__ == "__main__":
    main()
