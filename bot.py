import os
import threading
from flask import Flask
import discord
from discord.ext import commands
from discord.ui import Modal, TextInput, View, Button

# ================= [Render 웹 포트 열기 (24시간 유지용)] =================
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()
# =========================================================================

# ================= [내 서버 정보 설정] =================
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = 1462029945034375181            # 내 서버 ID[cite: 9]
REQUEST_CHANNEL_ID = 1567519631994781717  # #디코사용요청 채널 ID[cite: 9]
LOG_CHANNEL_ID = 1569681824660328520      # #진영닉넴답변-로그 채널 ID[cite: 9]

ROLE_ADD_ID = 1462037847757295789         # 부여할 역할 (시청자) ID[cite: 9]
ROLE_REMOVE_ID = 1565700634009665726      # 회수할 역할 (권한대기) ID[cite: 9]
# =======================================================

class VerifyModal(Modal, title="등업 신청서"):
    faction = TextInput(
        label="진영을 입력해주세요 (호드 / 얼라이언스)",
        placeholder="호드 또는 얼라이언스 입력",
        min_length=2,
        max_length=10,
        required=True
    )
    character_name = TextInput(
        label="인게임 캐릭터명을 입력해주세요",
        placeholder="정확한 캐릭터명 입력",
        min_length=2,
        max_length=12,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        
        # 1. 역할 부여 및 회수 처리 (디스코드 권한 계층 오류 방지)
        add_role = guild.get_role(ROLE_ADD_ID)
        remove_role = guild.get_role(ROLE_REMOVE_ID) if ROLE_REMOVE_ID else None
        
        try:
            if add_role:
                await member.add_roles(add_role)
            if remove_role and remove_role in member.roles:
                await member.remove_roles(remove_role)
        except discord.Forbidden:
            print("경고: 봇의 역할 순서가 시청자/권한대기 역할보다 낮아 역할을 부여하지 못했습니다.")

        chosen_faction = self.faction.value.strip()
        char_name = self.character_name.value.strip()
            
        # 2. 유저 본인에게만 뜨는 완료 메시지 (개인 채널 생성 없음)
        await interaction.response.send_message(
            f"✅ **등업 신청이 완료되었습니다!**\n• 진영: {chosen_faction}\n• 캐릭터명: {char_name}", 
            ephemeral=True
        )
        
        # 3. 관리자 로그 채널로 답변 내용 전송
        log_channel = guild.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(
                title="📝 신규 등업 신청 로그",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="신청자 계정", value=f"{member.mention} ({member.name})", inline=False)
            embed.add_field(name="진영", value=chosen_faction, inline=True)
            embed.add_field(name="캐릭터명", value=char_name, inline=True)
            await log_channel.send(embed=embed)

class VerifyView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="등업 신청하기", style=discord.ButtonStyle.green, custom_id="verify_btn", emoji="📝")
    async def verify_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(VerifyModal())

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(VerifyView())

bot = MyBot()

@bot.event
async def on_ready():
    print(f"로그인 성공: {bot.user}")
    channel = bot.get_channel(REQUEST_CHANNEL_ID)
    if channel:
        history = [msg async for msg in channel.history(limit=10)]
        has_panel = any(msg.author == bot.user and msg.components for msg in history)
        if not has_panel:
            embed = discord.Embed(
                title="디스코드 등업 안내",
                description="아래 버튼을 눌러 **진영**과 **인게임 캐릭터명**을 입력해 주세요.\n제출 즉시 등업 처리가 진행됩니다.",
                color=discord.Color.blue()
            )
            await channel.send(embed=embed, view=VerifyView())
    # (기존에 들어있던 코드들 유지)
    bot.add_view(StreamerAuthView())  # 추가 1
    await bot.tree.sync()             # 추가 2
            
import random
import string
import time
import discord
from discord import app_commands
from discord.ext import commands

# ==========================================
# [설정 구역] 서버 환경에 맞게 ID 숫자만 수정하세요
# ==========================================
STREAMER_ROLE_ID = 1565697370044243978  # 부여할 '스트리머' 역할 ID
LOG_CHANNEL_ID = 1570823674594988132    # 인증 성공/실패 알림을 받을 관리자 로그 채널 ID
# ==========================================

# 12자리 대소문자 + 숫자 + 특수문자 랜덤 생성 함수
def generate_strong_code(length=12):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        if (any(c.islower() for c in code) and
            any(c.isupper() for c in code) and
            any(c.isdigit() for c in code) and
            any(c in "!@#$%^&*" for c in code)):
            return code

CURRENT_STREAMER_CODE = generate_strong_code(12)
FAILED_ATTEMPTS = {}

# 1. 코드 입력 팝업창 (Modal)
class StreamerAuthModal(discord.ui.Modal, title="스트리머 전용 권한 인증"):
    code_input = discord.ui.TextInput(
        label="전달받은 12자리 인증 코드를 입력하세요",
        placeholder="대·소문자 및 특수문자를 정확히 구분하여 입력",
        min_length=4,
        max_length=32,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        now = time.time()

        # 10분 쿨타임 체크
        if user_id in FAILED_ATTEMPTS:
            count, lock_until = FAILED_ATTEMPTS[user_id]
            if now < lock_until:
                remain = int((lock_until - now) // 60) + 1
                await interaction.response.send_message(
                    f"⛔ 입력 횟수 초과로 일시 차단되었습니다. **{remain}분 후** 다시 시도해 주세요.",
                    ephemeral=True
                )
                return
            elif lock_until != 0 and now >= lock_until:
                FAILED_ATTEMPTS[user_id] = [0, 0]

        entered = self.code_input.value.strip()
        log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)

        if entered == CURRENT_STREAMER_CODE:
            role = interaction.guild.get_role(STREAMER_ROLE_ID)
            if not role:
                await interaction.response.send_message("⚠️ 스트리머 역할을 찾을 수 없습니다. 관리자에게 문의하세요.", ephemeral=True)
                return

            if role in interaction.user.roles:
                await interaction.response.send_message("✅ 이미 스트리머 역할을 보유하고 계십니다.", ephemeral=True)
                return

            await interaction.user.add_roles(role, reason="스트리머 인증 코드 일치")
            FAILED_ATTEMPTS.pop(user_id, None)

            await interaction.response.send_message(
                "🎉 **인증이 완료되었습니다!** `스트리머` 역할이 부여되어 전용 음성채널 입장이 가능합니다.",
                ephemeral=True
            )

            if log_channel:
                embed = discord.Embed(
                    title="🎙️ 스트리머 권한 인증 완료",
                    description=f"**대상:** {interaction.user.mention} (`{interaction.user.id}`)\n**결과:** `스트리머` 역할 자동 부여 완료",
                    color=discord.Color.green()
                )
                await log_channel.send(embed=embed)

        else:
            count, _ = FAILED_ATTEMPTS.get(user_id, [0, 0])
            count += 1
            if count >= 3:
                FAILED_ATTEMPTS[user_id] = [count, now + 600]
                await interaction.response.send_message(
                    "❌ 인증 코드가 3회 연속 일치하지 않아 **10분간 입력이 차단**됩니다.",
                    ephemeral=True
                )
                if log_channel:
                    embed = discord.Embed(
                        title="🚨 스트리머 인증 시도 차단 알림",
                        description=f"**대상:** {interaction.user.mention} (`{interaction.user.id}`)\n**사유:** 잘못된 코드 3회 연속 입력 (10분 차단)",
                        color=discord.Color.red()
                    )
                    await log_channel.send(embed=embed)
            else:
                FAILED_ATTEMPTS[user_id] = [count, 0]
                await interaction.response.send_message(
                    f"❌ 인증 코드가 올바르지 않습니다. (남은 기회: **{3 - count}회**)",
                    ephemeral=True
                )

# 2. 채널에 띄워둘 영구 인증 버튼
class StreamerAuthView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="스트리머 권한 인증",
        style=discord.ButtonStyle.primary,
        emoji="🎙️",
        custom_id="persistent_streamer_auth_btn"
    )
    async def auth_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StreamerAuthModal())

# 3. 관리자 전용 명령어 모음
# (1) 인증 버튼 패널 설치 명령어
@bot.tree.command(name="스트리머패널설치", description="[관리자] 현재 채널에 스트리머 인증 버튼을 생성합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_streamer_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎙️ 스트리머 전용 권한 인증",
        description=(
            "운영진으로부터 전달받은 **12자리 고유 인증 코드**가 있는 스트리머분들은\n"
            "아래 버튼을 눌러 코드를 입력하시면 즉시 전용 권한 및 음성채널 입장이 활성화됩니다."
        ),
        color=discord.Color.purple()
    )
    await interaction.channel.send(embed=embed, view=StreamerAuthView())
    await interaction.response.send_message("✅ 스트리머 인증 패널이 설치되었습니다.", ephemeral=True)

# (2) 코드 발급 및 복사 버튼 출력 명령어 (/스트리머인증코드)
@bot.tree.command(name="스트리머인증코드", description="[관리자] 새 12자리 스트리머 인증 코드를 발급합니다. (복사 버튼 제공)")
@app_commands.describe(직접입력="원하는 코드가 있을 때만 입력 (비워두면 12자리 랜덤 코드가 자동 생성됩니다)")
@app_commands.checks.has_permissions(administrator=True)
async def manage_streamer_code(interaction: discord.Interaction, 직접입력: str = None):
    global CURRENT_STREAMER_CODE
    if 직접입력:
        CURRENT_STREAMER_CODE = 직접입력.strip()
        status_text = "입력하신 코드로 변경되었습니다."
    else:
        CURRENT_STREAMER_CODE = generate_strong_code(12)
        status_text = "새로운 **12자리 랜덤 코드**가 발급되었습니다. (이전 코드는 즉시 폐기됨)"

    # 여러 줄 코드 블록(```)을 사용하면 디스코드 우측 상단에 원클릭 복사 아이콘이 자동 생성됩니다.
    msg = (
        f"🔄 {status_text}\n"
        f"아래 박스 우측 상단의 **복사 아이콘**을 눌러 스트리머에게 전달하세요:\n"
        f"```\n{CURRENT_STREAMER_CODE}\n```"
    )

    await interaction.response.send_message(msg, ephemeral=True)
    
bot.run(TOKEN)
