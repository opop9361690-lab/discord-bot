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

bot.run(TOKEN)
