"""
Bot de Eventos Albion Online - STOMP
=====================================
Um bot, tres comandos:
  1. /ping-funcao   - painel de botoes para marcar sua funcao (cargo de notificacao,
                       fica marcado ate a pessoa clicar de novo pra remover).
  2. /ping-conteudo - cria um topico num canal (ex: #ping-pve, #ping-pvp) com a
                       imagem da composicao de referencia e vagas por reacao
                       (1 emoji por funcao, ordem de chegada, avisa quando lota).
  3. /evento        - cria um evento com call de voz, selecao de classe por botao
                       (com icone da arma), rastreia o tempo de presenca de cada
                       jogador e, ao final, divide o loot proporcional ao tempo.

Decisoes de projeto (assumidas por mim - ajuste se quiser outro comportamento):
  - O mesmo cargo do Discord ("Cargo Discord" na planilha) e usado tanto pro
    ping de funcao (fica marcado ate desmarcar) quanto pro evento (temporario:
    e removido quando a pessoa sai da call ou quando o evento termina).
  - No ping de funcao, uma pessoa pode se marcar em varias funcoes ao mesmo
    tempo (e um toggle por classe, independente).
  - So conta tempo de presenca quem clicou em algum botao de classe dentro do
    evento (quem entra na call "por fora" sem clicar nao e contado).
  - Se a pessoa nao estiver em nenhuma call, o bot nao consegue move-la
    (limitacao da API do Discord) - ela precisa entrar em qualquer call e
    clicar de novo.
  - Na divisao com comprador: o valor liquido (apos taxa de 15% e reparo) e
    dividido entre TODOS os participantes pela % de tempo, incluindo o
    comprador. Ou seja, o comprador paga pra cada pessoa o valor da tabela,
    exceto pra si mesmo (o proprio % dele fica com ele).
  - Roster de classes vem de classes.json (12 classes de exemplo em T4 pra
    already funcionar). Troque a qualquer momento com /classes importar
    (usa o modelo classes_evento_modelo.xlsx) ou /classes adicionar.

Requisitos:
    pip install discord.py requests pillow openpyxl --break-system-packages

Configuracao:
    1. Token do bot: defina a variavel de ambiente DISCORD_BOT_TOKEN,
       ou crie um arquivo token.txt (mesma pasta) so com o token dentro.
    2. Convide o bot no servidor com permissoes: Gerenciar Cargos, Gerenciar
       Canais, Mover Membros, Gerenciar Emojis e Figurinhas, Enviar Mensagens,
       Usar Comandos de Barra, Conectar, Ver Canais.
    3. Ative em Developer Portal > Bot > Privileged Gateway Intents:
       "Server Members Intent" (o bot usa pra gerenciar cargo/mover membro).
    4. Rode: python3 event_bot.py
"""

import asyncio
import base64
import copy
import io
import os
import time
import uuid
from datetime import datetime, timezone

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

from albion_shared import (
    BASE_DIR, GUILD_DATA_DIR, GUILD_TAG, API_SEARCH, RENDER_BASE, BUYER_FEE, GOLD, TIMEZONE,
    _load_json, _save_json, _slug, _fmt_duration, _fmt_prata, _looks_like_item_id,
    resolve_item_id_by_name, _parse_builds_texto, _parse_data_hora, _strip_accents,
    DEFAULT_CLASSES, load_classes, save_classes, _slots_from_legacy_classes,
    DEFAULT_CONTENT_TYPES, DEFAULT_TEMPLATES, load_templates, save_templates,
    load_content_types, save_content_types,
    load_events, save_events, _archive_event, compute_split,
    load_pings, save_pings, _parse_slots_texto, _assign_emojis,
    EQUIP_SLOTS, get_build_equip, load_history, load_items_db, get_item_abilities,
    load_reputation, save_reputation, add_rating, compute_reputation_stats,
    set_current_guild, list_tenant_guild_ids, get_current_guild,
    load_saldos, save_saldos, load_saldo_gestores, save_saldo_gestores,
    load_permissoes, set_permissao, nivel_permissao, ajustar_saldo,
    owner_discord_id_for_guild, is_saldo_gestor, add_saldo, quitar_saldo,
)

# =========================
# CONFIGURACAO
# =========================
# (dados/roster/templates/eventos moraram pra albion_shared.py - o site de
# configuracao usa o mesmo modulo, entao os dois leem/escrevem os mesmos
# arquivos JSON sem precisar sincronizar nada)

EVENTOS_CATEGORY_NAME = "\U0001F53A Eventos"


def _load_token():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if token:
        return token.strip()
    token_file = BASE_DIR / "token.txt"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "Token nao encontrado. Defina a variavel de ambiente DISCORD_BOT_TOKEN "
        "ou crie um arquivo token.txt (na mesma pasta do script) com o token dentro."
    )


TOKEN = _load_token()

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================
# ICONES / EMOJIS DE APLICACAO
# =========================

def fetch_item_icon_png(item_id, size=128):
    """Baixa o icone oficial do item Albion e devolve PNG pronto pra virar emoji."""
    url = f"{RENDER_BASE}/{item_id}.png?count=1&quality=1"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# =========================
# CARD DE COMPOSICAO (imagem de referencia pro /ping-conteudo)
# =========================

_FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def _card_font(bold, size):
    path = f"{_FONT_DIR}/DejaVuSans-Bold.ttf" if bold else f"{_FONT_DIR}/DejaVuSans.ttf"
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


_CARD_BG = (15, 17, 23)
_CARD_ROW = (27, 30, 43)
_CARD_GOLD = (226, 188, 98)
_CARD_TEXT = (233, 230, 218)
_CARD_DIM = (154, 154, 168)
_F_CARD_TITLE = _card_font(True, 30)
_F_CARD_SUB = _card_font(False, 16)
_F_CARD_SLOT = _card_font(True, 19)
_F_CARD_BUILD = _card_font(False, 15)
_F_CARD_VAGAS = _card_font(True, 15)


def render_comp_card(comp):
    """Gera um PNG limpo com os slots e a build (arma principal) de cada um,
    no visual dark+dourado do site - usado como imagem de referencia no
    topico criado por /ping-conteudo. Roda em thread separada (e sincrono,
    usa requests) - chame via asyncio.to_thread."""
    slots = comp.get("slots") or []
    largura = 680
    pad = 28
    altura_linha = 60
    altura_cabecalho = 96
    altura = altura_cabecalho + max(1, len(slots)) * altura_linha + pad

    img = Image.new("RGB", (largura, altura), _CARD_BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, largura, altura_cabecalho], fill=_CARD_ROW)
    draw.rectangle([0, altura_cabecalho - 3, largura, altura_cabecalho], fill=_CARD_GOLD)
    draw.text((pad, 22), (comp.get("nome") or "Composicao")[:40], font=_F_CARD_TITLE, fill=(255, 255, 255))
    draw.text((pad, 62), f"{len(slots)} slot(s) · composicao de referencia", font=_F_CARD_SUB, fill=_CARD_DIM)

    y = altura_cabecalho
    for i, slot in enumerate(slots):
        if i % 2 == 1:
            draw.rectangle([0, y, largura, y + altura_linha], fill=_CARD_ROW)

        builds = slot.get("builds") or []
        build = builds[0] if builds else None
        item_id = (build or {}).get("item_id") or slot.get("item_id")

        icon_size = 44
        icon_x, icon_y = pad, y + (altura_linha - icon_size) // 2
        if item_id:
            try:
                icon_bytes = fetch_item_icon_png(item_id, size=icon_size)
                icon_img = Image.open(io.BytesIO(icon_bytes)).convert("RGBA")
                img.paste(icon_img, (icon_x, icon_y), icon_img)
            except Exception:
                pass

        text_x = pad + icon_size + 16
        draw.text((text_x, y + 9), (slot.get("nome") or "?")[:36], font=_F_CARD_SLOT, fill=_CARD_TEXT)
        nomes_builds = " / ".join(b["nome"] for b in builds) if builds else "sem build definida"
        draw.text((text_x, y + 34), nomes_builds[:60], font=_F_CARD_BUILD, fill=_CARD_DIM)

        vagas = slot.get("vagas")
        vagas_txt = f"{vagas} vaga(s)" if vagas else "sem limite"
        bbox = draw.textbbox((0, 0), vagas_txt, font=_F_CARD_VAGAS)
        vw = bbox[2] - bbox[0]
        draw.text((largura - pad - vw, y + (altura_linha - 18) // 2), vagas_txt, font=_F_CARD_VAGAS, fill=_CARD_GOLD)

        y += altura_linha

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_build_detail_card(slot_nome, build):
    """Gera um print compacto do equipamento completo de UMA build (arma, elmo,
    peitoral, capa etc, o que tiver montado no paperdoll do site) - postado
    como mensagem separada no topico, pra quem for reagir ver exatamente o
    que a funcao usa antes de confirmar."""
    equip = get_build_equip(build)
    itens = [
        (sd["label"], equip[sd["key"]]["item"], equip[sd["key"]].get("swaps") or [])
        for sd in EQUIP_SLOTS if equip.get(sd["key"], {}).get("item")
    ]
    if not itens and build.get("item_id"):
        itens = [("Arma", build["item_id"], [])]

    tem_swap = any(swaps for _, _, swaps in itens)

    pad = 22
    icon = 52
    mini = 22
    gap = 16
    col_w = icon + gap
    n = max(1, len(itens))
    titulo = f"{slot_nome} · {build.get('nome') or '?'}"[:60]

    # a largura precisa caber tanto os icones quanto o titulo (senao o titulo
    # corta feio quando a build tem so 1-2 pecas equipadas)
    titulo_w = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), titulo, font=_F_CARD_SLOT)[2]
    icones_w = pad * 2 + n * col_w - gap
    largura = max(icones_w, titulo_w + pad * 2, 260)
    altura = 148 + (32 if tem_swap else 0)

    img = Image.new("RGB", (largura, altura), _CARD_BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, largura - 1, altura - 1], outline=_CARD_GOLD, width=2)
    draw.text((pad, 16), titulo, font=_F_CARD_SLOT, fill=_CARD_TEXT)

    x, y_icon = pad, 56
    if not itens:
        draw.text((pad, y_icon), "sem equipamento detalhado ainda", font=_F_CARD_BUILD, fill=_CARD_DIM)
    for label, item_id, swaps in itens:
        try:
            icon_bytes = fetch_item_icon_png(item_id, size=icon)
            icon_img = Image.open(io.BytesIO(icon_bytes)).convert("RGBA")
            fundo = Image.new("RGBA", (icon, icon), (*_CARD_ROW, 255))
            fundo.paste(icon_img, (0, 0), icon_img)
            img.paste(fundo, (x, y_icon))
        except Exception:
            draw.rectangle([x, y_icon, x + icon, y_icon + icon], fill=_CARD_ROW)
        bbox = draw.textbbox((0, 0), label, font=_F_CARD_BUILD)
        lw = bbox[2] - bbox[0]
        label_y = y_icon + icon + 8
        draw.text((x + icon / 2 - lw / 2, label_y), label, font=_F_CARD_BUILD, fill=_CARD_DIM)

        if swaps:
            sy = label_y + 21
            sx = x
            draw.text((sx, sy + 3), "⇄", font=_F_CARD_BUILD, fill=_CARD_GOLD)
            sx += 16
            for swap_id in swaps[:3]:  # no maximo 3 mini icones, senao esperta demais
                try:
                    swap_bytes = fetch_item_icon_png(swap_id, size=mini)
                    swap_img = Image.open(io.BytesIO(swap_bytes)).convert("RGBA")
                    swap_fundo = Image.new("RGBA", (mini, mini), (*_CARD_BG, 255))
                    swap_fundo.paste(swap_img, (0, 0), swap_img)
                    img.paste(swap_fundo, (sx, sy))
                    draw.rectangle([sx, sy, sx + mini, sy + mini], outline=_CARD_GOLD, width=1)
                except Exception:
                    pass
                sx += mini + 4

        x += col_w

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def resolve_item_id_by_name(nome_arma):
    """Usa a busca publica do Albion pra achar o Item ID a partir do nome da arma."""
    try:
        resp = requests.get(API_SEARCH, params={"q": nome_arma}, timeout=15)
        resp.raise_for_status()
        items = (resp.json() or {}).get("items") or []
        if items:
            return items[0].get("UniqueName")
    except Exception:
        pass
    return None


async def _get_application_id():
    if bot.application_id:
        return bot.application_id
    info = await bot.application_info()
    return info.id


async def ensure_class_emoji(class_entry):
    """Sobe o icone da arma como emoji da aplicacao (fica disponivel em qualquer
    servidor onde o bot estiver) e cacheia o ID em classes.json. Se falhar por
    qualquer motivo, o botao so fica sem icone - nunca quebra o bot por causa disso."""
    if class_entry.get("emoji_id"):
        return discord.PartialEmoji(name=_slug(class_entry["nome"]), id=int(class_entry["emoji_id"]))

    item_id = class_entry.get("item_id")
    if not item_id and class_entry.get("arma_nome"):
        item_id = resolve_item_id_by_name(class_entry["arma_nome"])
        class_entry["item_id"] = item_id

    if not item_id:
        return None

    try:
        png_bytes = await asyncio.to_thread(fetch_item_icon_png, item_id)
        b64 = base64.b64encode(png_bytes).decode()
        app_id = await _get_application_id()
        url = f"https://discord.com/api/v10/applications/{app_id}/emojis"
        headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
        payload = {"name": _slug(class_entry["nome"]), "image": f"data:image/png;base64,{b64}"}
        resp = await asyncio.to_thread(lambda: requests.post(url, headers=headers, json=payload, timeout=20))
        if resp.status_code in (200, 201):
            emoji_data = resp.json()
            class_entry["emoji_id"] = emoji_data["id"]
            return discord.PartialEmoji(name=emoji_data["name"], id=int(emoji_data["id"]))
        print(f"[aviso] Falha ao subir emoji de {class_entry['nome']}: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[aviso] Erro ao gerar emoji de {class_entry['nome']}: {e}")
    return None


EVENT_STATUS_ACTIVE = 2
EVENT_STATUS_COMPLETED = 3


def _create_live_event_sync(guild_id, titulo, local, quando_ts, duracao_horas=3):
    """Cria um Evento Agendado do Discord pro ping. Se quando_ts for None, o
    evento comeca em ~1min e ja forcamos status ACTIVE (banner 'Acontecendo
    agora' aparece na hora). Se tiver horario marcado, so agenda pra aquele
    horario (o Discord mostra "em breve" e o banner aparece sozinho quando
    chegar a hora, sem precisar forcar). Nunca derruba o fluxo do ping se der
    erro - so retorna None e loga."""
    try:
        headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
        now = datetime.now(timezone.utc)
        ao_vivo = quando_ts is None
        start = now + __import__("datetime").timedelta(seconds=60) if ao_vivo else datetime.fromtimestamp(quando_ts, tz=timezone.utc)
        if start <= now:
            start = now + __import__("datetime").timedelta(seconds=60)
        end = start + __import__("datetime").timedelta(hours=max(1, min(duracao_horas, 12)))

        payload = {
            "name": titulo[:100],
            "privacy_level": 2,
            "scheduled_start_time": start.isoformat(),
            "scheduled_end_time": end.isoformat(),
            "entity_type": 3,  # EXTERNAL
            "entity_metadata": {"location": (local or "Albion Online")[:100]},
        }
        url = f"https://discord.com/api/v10/guilds/{guild_id}/scheduled-events"
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code not in (200, 201):
            print(f"[aviso] falha ao criar evento ao vivo do ping: {resp.status_code} {resp.text[:200]}")
            return None
        event = resp.json()
        event_id = event["id"]

        if ao_vivo:
            patch_url = f"{url}/{event_id}"
            resp2 = requests.patch(patch_url, headers=headers, json={"status": EVENT_STATUS_ACTIVE}, timeout=15)
            if resp2.status_code not in (200, 201):
                print(f"[aviso] evento criado mas nao ativado: {resp2.status_code} {resp2.text[:200]}")
        return event_id
    except Exception as e:
        print(f"[aviso] erro criando evento ao vivo do ping: {e}")
        return None


async def create_live_event(ping):
    """Wrapper async do _create_live_event_sync, chamado na criacao do ping."""
    event_id = await asyncio.to_thread(
        _create_live_event_sync, ping["guild_id"], ping["titulo"], ping.get("local"), ping.get("quando"),
    )
    if event_id:
        ping["event_id"] = event_id
    return event_id


def _complete_live_event_sync(guild_id, event_id):
    try:
        headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
        url = f"https://discord.com/api/v10/guilds/{guild_id}/scheduled-events/{event_id}"
        resp = requests.patch(url, headers=headers, json={"status": EVENT_STATUS_COMPLETED}, timeout=15)
        if resp.status_code not in (200, 201):
            print(f"[aviso] falha ao encerrar evento ao vivo do ping: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[aviso] erro encerrando evento ao vivo do ping: {e}")


async def complete_live_event(ping):
    """Wrapper async, chamado no encerramento do ping. So faz algo se o ping
    tiver um event_id salvo (pings antigos, de antes dessa feature, nao tem)."""
    event_id = ping.get("event_id")
    if not event_id:
        return
    await asyncio.to_thread(_complete_live_event_sync, ping["guild_id"], event_id)



def _partial_emoji_from_cache(class_entry):
    if class_entry.get("emoji_id"):
        return discord.PartialEmoji(name=_slug(class_entry["nome"]), id=int(class_entry["emoji_id"]))
    return None


def _row_for(ordem):
    return min(max(ordem - 1, 0) // 5, 4)


# =========================
# CARGOS
# =========================

def _persist_class_update(class_entry):
    """Tenta gravar role_id/emoji_id de volta na origem (classes.json ou dentro de um
    slot de templates.json). Se o slot for avulso (ex: build escolhida na hora), so
    ignora - o ensure_role ja resolve por nome do cargo na proxima vez sem problema."""
    classes = load_classes()
    for c in classes:
        if c["id"] == class_entry["id"]:
            c.update(class_entry)
            save_classes(classes)
            return
    templates = load_templates()
    for t in templates:
        for s in t["slots"]:
            if s["id"] == class_entry["id"]:
                s["role_id"] = class_entry.get("role_id", s.get("role_id"))
                save_templates(templates)
                return


async def ensure_role(guild, class_entry):
    role_id = class_entry.get("role_id")
    if role_id:
        role = guild.get_role(int(role_id))
        if role:
            return role
    existing = discord.utils.get(guild.roles, name=class_entry["cargo"])
    if existing:
        class_entry["role_id"] = existing.id
        _persist_class_update(class_entry)
        return existing
    role = await guild.create_role(name=class_entry["cargo"], mentionable=True, reason="Cargo de classe do bot de eventos")
    class_entry["role_id"] = role.id
    _persist_class_update(class_entry)
    return role


# =========================
# PING DE FUNCAO
# =========================

class FuncaoPingView(discord.ui.View):
    def __init__(self, classes):
        super().__init__(timeout=None)
        for c in classes[:25]:
            btn = discord.ui.Button(
                label=c["nome"],
                style=discord.ButtonStyle.secondary,
                custom_id=f"funcping:{get_current_guild()}:{c['id']}",
                emoji=_partial_emoji_from_cache(c),
                row=_row_for(c.get("ordem", 1)),
            )
            btn.callback = self._make_callback(c)
            self.add_item(btn)

    def _make_callback(self, class_entry):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            await handle_funcping_click(interaction, class_entry)
        return callback


async def handle_funcping_click(interaction: discord.Interaction, class_entry):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    member = interaction.user
    classes = load_classes()
    entry = next((c for c in classes if c["id"] == class_entry["id"]), class_entry)
    role = await ensure_role(guild, entry)
    if role in member.roles:
        await member.remove_roles(role, reason="Ping de funcao - desmarcado")
        await interaction.followup.send(f"❌ Removido de **{entry['nome']}**.", ephemeral=True)
    else:
        await member.add_roles(role, reason="Ping de funcao - marcado")
        await interaction.followup.send(f"\U0001F514 Marcado como **{entry['nome']}**!", ephemeral=True)
        try:
            await interaction.channel.send(f"\U0001F514 {member.mention} se marcou como **{entry['nome']}** ({role.mention}).")
        except Exception:
            pass


# =========================
# EVENTO - PAINEL DE SLOTS (CLASSES OU COMPOSICAO)
# =========================

def _slots_from_legacy_classes(classes):
    """Roster simples (classes.json) tratado como slots de 1 build so - mantem o
    comportamento antigo identico (clique = marca na hora, sem sub-menu de build)."""
    slots = []
    for c in classes:
        slots.append({
            "id": c["id"], "nome": c["nome"], "cargo": c["cargo"], "vagas": c.get("vagas"),
            "ordem": c.get("ordem", 1), "role_id": c.get("role_id"),
            "item_id": c.get("item_id"), "emoji_id": c.get("emoji_id"),
            "builds": [{"nome": c["nome"], "item_id": c.get("item_id"), "arma_nome": c.get("arma_nome"), "emoji_id": c.get("emoji_id")}],
        })
    return slots


async def _build_event_slots(template_id):
    """Monta os slots do painel do evento. Sem template = roster simples de sempre
    (classes.json). Com template = composicao com slots/builds (comp criado via /comp).
    Devolve None se o template pedido nao existir."""
    if template_id:
        templates = load_templates()
        tpl = next((t for t in templates if t["id"] == template_id), None)
        if not tpl:
            return None
        slots = copy.deepcopy(tpl["slots"])
        for s in slots:
            for b in s["builds"]:
                await ensure_class_emoji(b)
            s["item_id"] = s["builds"][0].get("item_id")
            s["emoji_id"] = s["builds"][0].get("emoji_id")
        save_templates(templates)  # persiste emoji_id resolvido nas builds originais tambem
        return slots
    classes = load_classes()
    for c in classes:
        await ensure_class_emoji(c)
    save_classes(classes)
    return _slots_from_legacy_classes(classes)


def build_evento_embed(event_id):
    events = load_events()
    event = events[event_id]
    lines = []
    for c in event["classes"]:
        # marca quem ja clicou nessa vaga (@ de cada um), igual o painel do
        # ping ja fazia - antes so mostrava o numero, sem dizer quem era
        ocupados_uids = [uid for uid, p in event["participantes"].items() if p["classe_id"] == c["id"]]
        vagas_txt = f"{len(ocupados_uids)}/{c['vagas']}" if c.get("vagas") else f"{len(ocupados_uids)}"
        builds = c.get("builds") or []
        build_txt = f" _({' / '.join(b['nome'] for b in builds)})_" if len(builds) > 1 else ""
        linha = f"**{c['nome']}**{build_txt} — {vagas_txt}"
        if ocupados_uids:
            linha += "\n> " + " ".join(f"<@{uid}>" for uid in ocupados_uids)
        lines.append(linha)

    if event.get("status") == "agendado":
        quando_dt = datetime.fromtimestamp(event["quando"], TIMEZONE)
        status_txt = f"\U0001F4C5 Agendado pra **{quando_dt.strftime('%d/%m às %H:%M')}** (horario de Brasilia) — a call e criada automaticamente na hora"
    elif event.get("contagem_iniciada"):
        status_txt = "\U0001F534 Ao vivo agora — contagem de tempo iniciada"
    else:
        status_txt = "\U0001F7E1 Sala aberta — aguardando o organizador dar `/iniciar-evento` pra comecar a contagem"

    embed = discord.Embed(
        title=f"\U0001F53A Evento: {event['nome']}",
        description=f"{status_txt}\n\nClique na sua classe/slot abaixo pra se inscrever." + "\n\n" + "\n".join(lines),
        color=GOLD,
    )
    embed.set_footer(text=f"Guild {GUILD_TAG} · Bot criado por Jeverson")
    return embed


async def refresh_evento_panel(event_id):
    """Reedita a mensagem do painel do evento com a contagem de vagas atualizada."""
    events = load_events()
    event = events.get(event_id)
    if not event or not event.get("mensagem_id"):
        return
    try:
        channel = bot.get_channel(event["canal_texto_id"]) or await bot.fetch_channel(event["canal_texto_id"])
        msg = await channel.fetch_message(event["mensagem_id"])
        await msg.edit(embed=build_evento_embed(event_id), view=EventoView(event_id))
    except Exception as e:
        print(f"[aviso] Falha ao atualizar painel do evento {event_id}: {e}")


class EventoView(discord.ui.View):
    def __init__(self, event_id):
        super().__init__(timeout=None)
        events = load_events()
        event = events.get(event_id)
        if not event:
            return
        participantes = event["participantes"]
        # ate 20 classes (nao 25) pra sempre sobrar a linha 4 pro botao de
        # Iniciar evento, igual o PingFuncaoView reserva espaco pro Sair/Config.
        for c in event["classes"][:20]:
            ocupadas = sum(1 for p in participantes.values() if p["classe_id"] == c["id"])
            vagas = c.get("vagas")
            label = f"{c['nome']} ({ocupadas}/{vagas})" if vagas else c["nome"]
            full = vagas is not None and ocupadas >= vagas
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success if full else discord.ButtonStyle.secondary,
                custom_id=f"evento:{event_id}:{c['id']}",
                emoji=_partial_emoji_from_cache(c),
                row=_row_for(c.get("ordem", 1)),
            )
            btn.callback = self._make_callback(event_id, c)
            self.add_item(btn)

        if event.get("status") == "aberto" and not event.get("contagem_iniciada"):
            start_btn = discord.ui.Button(
                label="▶️ Iniciar evento",
                style=discord.ButtonStyle.success,
                custom_id=f"evtstart:{event_id}",
                row=4,
            )
            start_btn.callback = self._make_start_callback(event_id)
            self.add_item(start_btn)

    def _make_callback(self, event_id, slot):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            await handle_evento_click(interaction, event_id, slot)
        return callback

    def _make_start_callback(self, event_id):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            await handle_iniciar_evento_button(interaction, event_id)
        return callback


class BuildChoiceView(discord.ui.View):
    """Sub-menu efemero que aparece quando o slot tem mais de uma build possivel (swap)."""
    def __init__(self, event_id, slot, builds):
        super().__init__(timeout=120)
        for b in builds[:25]:
            btn = discord.ui.Button(label=b["nome"][:80], style=discord.ButtonStyle.secondary, emoji=_partial_emoji_from_cache(b))
            btn.callback = self._make_callback(event_id, slot, b)
            self.add_item(btn)

    def _make_callback(self, event_id, slot, build):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            await interaction.response.defer(ephemeral=True)
            await _finalize_slot_assignment(interaction, event_id, slot, build)
        return callback


async def handle_evento_click(interaction: discord.Interaction, event_id, slot):
    events = load_events()
    event = events.get(event_id)
    if not event or event.get("status") not in ("aberto", "agendado"):
        await interaction.response.send_message("Esse evento ja foi encerrado.", ephemeral=True)
        return

    participantes = event["participantes"]
    uid = str(interaction.user.id)
    vagas = slot.get("vagas")
    ocupadas = sum(1 for p in participantes.values() if p["classe_id"] == slot["id"])
    ja_esta_nesse = uid in participantes and participantes[uid]["classe_id"] == slot["id"]
    if vagas is not None and ocupadas >= vagas and not ja_esta_nesse:
        await interaction.response.send_message(f"❌ Vagas de **{slot['nome']}** cheias.", ephemeral=True)
        return

    builds = slot.get("builds") or [{"nome": slot["nome"], "item_id": slot.get("item_id"), "arma_nome": None, "emoji_id": slot.get("emoji_id")}]
    if len(builds) == 1:
        await interaction.response.defer()
        await _finalize_slot_assignment(interaction, event_id, slot, builds[0])
    else:
        view = BuildChoiceView(event_id, slot, builds)
        await interaction.response.send_message(f"Escolha sua build pra **{slot['nome']}**:", view=view, ephemeral=True)


async def _finalize_slot_assignment(interaction: discord.Interaction, event_id, slot, build):
    """Registra a escolha, atribui cargo, tenta mover pra call (se ja existir) e
    reedita o painel. Usada tanto no clique direto (slot de 1 build) quanto depois
    da escolha no sub-menu de builds (swap)."""
    events = load_events()
    event = events.get(event_id)
    if not event or event.get("status") not in ("aberto", "agendado"):
        await interaction.followup.send("Esse evento ja foi encerrado.", ephemeral=True)
        return

    participantes = event["participantes"]
    uid = str(interaction.user.id)
    if uid not in participantes:
        participantes[uid] = {
            "nome": interaction.user.display_name,
            "classe_id": slot["id"],
            "build": build,
            "tempo_total": 0.0,
            "entrou_em": None,
        }
    else:
        participantes[uid]["classe_id"] = slot["id"]
        participantes[uid]["build"] = build
    save_events(events)

    guild = interaction.guild
    member = interaction.user
    role = await ensure_role(guild, slot)
    for s in event["classes"]:
        if s["id"] != slot["id"] and s.get("role_id"):
            old_role = guild.get_role(int(s["role_id"]))
            if old_role and old_role in member.roles:
                try:
                    await member.remove_roles(old_role, reason="Trocou de slot no evento")
                except Exception:
                    pass
    if role not in member.roles:
        try:
            await member.add_roles(role, reason="Slot do evento")
        except Exception:
            pass

    aviso_mover = ""
    voice_channel = guild.get_channel(event["canal_voz_id"]) if event.get("canal_voz_id") else None
    if voice_channel:
        if member.voice and member.voice.channel and member.voice.channel.id != voice_channel.id:
            try:
                await member.move_to(voice_channel, reason="Entrou no evento")
            except Exception:
                aviso_mover = "\n⚠️ Nao consegui te mover — entre em qualquer call e clique de novo."
        elif not member.voice:
            aviso_mover = f"\n⚠️ Entre em qualquer canal de voz e clique de novo pra eu te mover pra {voice_channel.mention}."
    elif event.get("status") == "agendado":
        aviso_mover = "\n(evento agendado - a call e criada automaticamente na hora marcada)"

    await refresh_evento_panel(event_id)
    build_txt = f" ({build['nome']})" if build.get("nome") and build["nome"] != slot["nome"] else ""
    await interaction.followup.send(f"✅ Marcado como **{slot['nome']}{build_txt}**!{aviso_mover}", ephemeral=True)


# =========================
# FINALIZACAO / DIVISAO DE LOOT
# =========================

def compute_split(participantes_tempo: dict, profit: float, has_buyer: bool, reparo: float = 0.0, taxa_pct: float = None):
    """Divide o profit proporcionalmente ao tempo de presenca de cada um.
    Formula confirmada: pct_i = tempo_i / soma(tempo de todos) -> sempre soma 100%.
    Exemplo confirmado: A fica 1h, B fica 30min -> A = 66.7%, B = 33.3%.
    taxa_pct: percentual da taxa de venda cobrada pelo comprador (ex: 15 = 15%),
    perguntado no modal pra quem fecha a divisao. Se nao informado, cai no
    BUYER_FEE padrao (compatibilidade)."""
    total = sum(participantes_tempo.values())
    if total <= 0:
        raise ValueError("Sem tempo registrado.")
    taxa = (taxa_pct / 100) if taxa_pct is not None else BUYER_FEE
    liquido = (profit - reparo) * (1 - taxa) if has_buyer else profit
    shares = {uid: {"pct": t / total, "valor": liquido * (t / total)} for uid, t in participantes_tempo.items()}
    return liquido, shares


class FinalizarLootView(discord.ui.View):
    def __init__(self, event_id):
        super().__init__(timeout=600)
        self.event_id = event_id

    @discord.ui.button(label="\U0001F4B0 Definir valor do profit", style=discord.ButtonStyle.success)
    async def definir_profit(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(ProfitModal(self.event_id))


class ProfitModal(discord.ui.Modal, title="Valor do profit"):
    profit = discord.ui.TextInput(label="Valor total do profit (prata)", placeholder="Ex: 5000000")

    def __init__(self, event_id):
        super().__init__()
        self.event_id = event_id

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        try:
            valor = float(str(self.profit.value).replace(".", "").replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Valor invalido. Use so numeros.", ephemeral=True)
            return
        view = CompradorView(self.event_id, valor)
        await interaction.response.send_message(
            f"Profit definido: **{_fmt_prata(valor)} prata**.\n\nAlguem vai comprar o loot?",
            view=view,
        )


class CompradorView(discord.ui.View):
    def __init__(self, event_id, profit):
        super().__init__(timeout=600)
        self.event_id = event_id
        self.profit = profit

    @discord.ui.button(label="Sim, alguem vai comprar", style=discord.ButtonStyle.primary)
    async def sim(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(ReparoModal(self.event_id, self.profit))

    @discord.ui.button(label="Nao, dividir tudo", style=discord.ButtonStyle.secondary)
    async def nao(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await finalizar_divisao(interaction, self.event_id, self.profit, has_buyer=False, reparo=0)

    @discord.ui.button(label="💾 Saldar (guardar saldo)", style=discord.ButtonStyle.blurple)
    async def saldar(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(SaldarReparoModal(self.event_id, self.profit))


class ReparoModal(discord.ui.Modal, title="Valor do reparo"):
    reparo = discord.ui.TextInput(label="Valor do reparo (prata)", placeholder="Ex: 300000")
    taxa_pct = discord.ui.TextInput(label="Taxa de venda do comprador (%)", placeholder="Ex: 15", default="15")

    def __init__(self, event_id, profit):
        super().__init__()
        self.event_id = event_id
        self.profit = profit

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        try:
            valor_reparo = float(str(self.reparo.value).replace(".", "").replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Valor invalido. Use so numeros.", ephemeral=True)
            return
        try:
            taxa = float(str(self.taxa_pct.value).replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Porcentagem invalida. Use so numeros (ex: 15).", ephemeral=True)
            return
        await finalizar_divisao(interaction, self.event_id, self.profit, has_buyer=True, reparo=valor_reparo, taxa_pct=taxa)


class SaldarReparoModal(discord.ui.Modal, title="Reparo e taxa (saldar)"):
    """Mesmos campos do ReparoModal, mas pro fluxo de Saldar: a guild credita
    o saldo de cada um ja descontando reparo/taxa de venda (ex: guild vende o
    loot e paga em prata pros participantes) - se nao teve custo nenhum,
    e so deixar reparo 0 e taxa 0."""
    reparo = discord.ui.TextInput(label="Valor do reparo (prata)", placeholder="Ex: 300000 (ou 0 se nao teve)", default="0")
    taxa_pct = discord.ui.TextInput(label="Taxa de venda (%)", placeholder="Ex: 15 (ou 0 se nao teve)", default="0")

    def __init__(self, event_id, profit):
        super().__init__()
        self.event_id = event_id
        self.profit = profit

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        try:
            valor_reparo = float(str(self.reparo.value).replace(".", "").replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Valor invalido. Use so numeros.", ephemeral=True)
            return
        try:
            taxa = float(str(self.taxa_pct.value).replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Porcentagem invalida. Use so numeros (ex: 15, ou 0).", ephemeral=True)
            return
        await saldar_divisao(interaction, self.event_id, self.profit, reparo=valor_reparo, taxa_pct=taxa)


async def finalizar_divisao(interaction, event_id, profit, has_buyer, reparo, taxa_pct=None):
    events = load_events()
    history = load_history()
    event = events.get(event_id) or history.get(event_id)
    if not event:
        await interaction.response.send_message("Evento nao encontrado (talvez ja tenha sido arquivado).", ephemeral=True)
        return

    tempos = {uid: p["tempo_total"] for uid, p in event["participantes"].items()}
    try:
        liquido, shares = compute_split(tempos, profit, has_buyer, reparo, taxa_pct)
    except ValueError:
        await interaction.response.send_message("Sem tempo registrado — nao da pra dividir.", ephemeral=True)
        return

    linhas = []
    for uid, s in sorted(shares.items(), key=lambda kv: kv[1]["valor"], reverse=True):
        nome = event["participantes"][uid]["nome"]
        linhas.append(f"{nome:<20} {s['pct']*100:5.1f}%   {_fmt_prata(s['valor']):>14} prata")

    taxa_exibida = taxa_pct if taxa_pct is not None else BUYER_FEE * 100
    aviso_taxa = f"\nTaxa de {taxa_exibida:.0f}% descontada do valor (reparo: {_fmt_prata(reparo)} prata)." if has_buyer else ""
    embed = discord.Embed(
        title="\U0001F4B0 Divisao do loot",
        description=(
            f"Profit bruto: {_fmt_prata(profit)} prata{aviso_taxa}\n"
            f"Valor a dividir: **{_fmt_prata(liquido)} prata**\n\n"
            "```\n" + "\n".join(linhas) + "\n```"
        ),
        color=0x2ECC71,
    )
    if has_buyer:
        embed.set_footer(text="O comprador paga o valor acima pra cada pessoa (exceto pra si mesmo).")
    embed.timestamp = discord.utils.utcnow()

    if event_id in events:
        _archive_event(event_id, events)

    await interaction.response.edit_message(content=None, embed=embed, view=None)


async def saldar_divisao(interaction, event_id, profit, reparo=0.0, taxa_pct=None):
    """Em vez de so mostrar quanto cada um deveria receber (pra combinar o
    pagamento por fora), credita o valor de cada um no saldo acumulado dentro
    do bot - ja descontando reparo/taxa de venda quando a guild vende o loot
    e paga em prata (reparo=0 e taxa_pct=0/None credita o valor cheio, igual
    antes)."""
    events = load_events()
    history = load_history()
    event = events.get(event_id) or history.get(event_id)
    if not event:
        await interaction.response.send_message("Evento nao encontrado (talvez ja tenha sido arquivado).", ephemeral=True)
        return

    tempos = {uid: p["tempo_total"] for uid, p in event["participantes"].items()}
    tem_desconto = bool(reparo) or bool(taxa_pct)
    try:
        liquido, shares = compute_split(tempos, profit, has_buyer=tem_desconto, reparo=reparo, taxa_pct=taxa_pct)
    except ValueError:
        await interaction.response.send_message("Sem tempo registrado — nao da pra dividir.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    linhas = []
    for uid, s in sorted(shares.items(), key=lambda kv: kv[1]["valor"], reverse=True):
        nome = event["participantes"][uid]["nome"]
        novo_saldo = add_saldo(guild_id, uid, nome, s["valor"], motivo=f"Loot: evento {event.get('nome') or event_id}")
        linhas.append(f"{nome:<20} +{_fmt_prata(s['valor']):>14} prata   (saldo: {_fmt_prata(novo_saldo)})")

    aviso_desconto = f"\nTaxa de {(taxa_pct or 0):.0f}% + reparo de {_fmt_prata(reparo)} prata descontados antes de creditar." if tem_desconto else ""
    embed = discord.Embed(
        title="\U0001F4BE Loot saldado",
        description=(
            f"Profit: {_fmt_prata(profit)} prata{aviso_desconto}\n"
            f"Valor creditado: **{_fmt_prata(liquido)} prata**\n\n"
            "```\n" + "\n".join(linhas) + "\n```\n"
            "Cada um pode conferir com `/saldo membro`. Ninguem precisa pagar agora."
        ),
        color=0x5865F2,
    )
    embed.timestamp = discord.utils.utcnow()

    if event_id in events:
        _archive_event(event_id, events)

    await interaction.response.edit_message(content=None, embed=embed, view=None)


# =========================
# COMANDOS
# =========================

classes_group = app_commands.Group(name="classes", description="Gerenciar as classes/armas do roster de eventos")
evento_group = app_commands.Group(name="evento", description="Comandos de evento")
comp_group = app_commands.Group(name="comp", description="Gerenciar composicoes (templates de evento com slots e builds/swap)")
saldo_group = app_commands.Group(name="saldo", description="Comandos de saldo")
permissao_group = app_commands.Group(name="permissao", description="[admin] Quem da equipe pode pingar e quem mexe no saldo")


# ---- niveis de permissao (ver albion_shared.nivel_permissao) ----
def _membro(user, guild):
    if guild is None or user is None:
        return None
    if isinstance(user, discord.Member):
        return user
    return guild.get_member(getattr(user, "id", user))


def _nivel(user, guild):
    m = _membro(user, guild)
    if m is None:
        return None
    p = m.guild_permissions
    return nivel_permissao(guild.id, m.id, [r.id for r in m.roles], p.administrator or p.manage_guild)


def _pode_pingar(user, guild):
    m = _membro(user, guild)
    if m is None:
        return False
    if m.guild_permissions.manage_roles:  # quem ja podia antes continua podendo
        return True
    return _nivel(m, guild) in ("admin", "gestor", "caller")


def _pode_saldo(user, guild):
    return _nivel(user, guild) in ("admin", "gestor")


def _eh_admin(user, guild):
    return _nivel(user, guild) == "admin"


class SemPermissao(app_commands.CheckFailure):
    pass


def requer_pingar():
    async def pred(interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        if _pode_pingar(interaction.user, interaction.guild):
            return True
        raise SemPermissao(
            "❌ Só caller, gestor de saldo ou admin do servidor pode usar esse comando. "
            "Peça pra um admin te liberar com `/permissao adicionar`."
        )
    return app_commands.check(pred)


MSG_SEM_SALDO = (
    "❌ Só gestor de saldo ou admin do servidor mexe no saldo. "
    "Um admin pode te liberar com `/permissao adicionar nivel:gestor`."
)


@classes_group.command(name="listar", description="Lista as classes configuradas")
async def classes_listar(interaction: discord.Interaction):
    set_current_guild(interaction.guild_id)
    classes = load_classes()
    if not classes:
        await interaction.response.send_message("Nenhuma classe configurada ainda.", ephemeral=True)
        return
    linhas = [f"{c['ordem']:>2}. {c['nome']:<20} item: {c.get('item_id') or '(nenhum)':<22} cargo: {c['cargo']}" for c in classes]
    await interaction.response.send_message("```\n" + "\n".join(linhas) + "\n```", ephemeral=True)


@classes_group.command(name="adicionar", description="Adiciona uma classe ao roster (max 25 no total)")
@app_commands.describe(
    nome="Nome da classe (aparece no botao)",
    cargo="Nome do cargo do Discord (criado automaticamente se nao existir)",
    item_id="Item ID do Albion, se souber (ex: T8_2H_HOLYSTAFF)",
    arma_nome="Nome da arma em portugues, se nao souber o Item ID",
    vagas="Limite de vagas nessa classe (deixe vazio = sem limite)",
)
@requer_pingar()
async def classes_adicionar(interaction: discord.Interaction, nome: str, cargo: str, item_id: str = None, arma_nome: str = None, vagas: int = None):
    set_current_guild(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)
    classes = load_classes()
    if len(classes) >= 25:
        await interaction.followup.send("❌ Limite de 25 classes atingido (limite de botoes por mensagem do Discord).", ephemeral=True)
        return
    novo = {
        "id": _slug(nome) or f"classe_{len(classes) + 1}",
        "nome": nome,
        "item_id": item_id,
        "arma_nome": arma_nome,
        "cargo": cargo,
        "vagas": vagas,
        "ordem": len(classes) + 1,
        "role_id": None,
        "emoji_id": None,
    }
    await ensure_class_emoji(novo)
    classes.append(novo)
    save_classes(classes)
    await interaction.followup.send(f"✅ Classe **{nome}** adicionada.", ephemeral=True)


@classes_group.command(name="remover", description="Remove uma classe do roster pelo nome")
@requer_pingar()
async def classes_remover(interaction: discord.Interaction, nome: str):
    set_current_guild(interaction.guild_id)
    classes = load_classes()
    novas = [c for c in classes if c["nome"].lower() != nome.lower()]
    if len(novas) == len(classes):
        await interaction.response.send_message("Classe nao encontrada.", ephemeral=True)
        return
    for i, c in enumerate(novas, start=1):
        c["ordem"] = i
    save_classes(novas)
    await interaction.response.send_message(f"\U0001F5D1️ Classe **{nome}** removida.", ephemeral=True)


# --- painel interativo (dropdown + botoes + modal), alternativa a decorar comando ---

class ClasseModal(discord.ui.Modal):
    def __init__(self, class_entry=None):
        super().__init__(title="Editar classe" if class_entry else "Nova classe")
        self.class_entry = class_entry
        self.nome_input = discord.ui.TextInput(
            label="Nome da classe", max_length=45,
            default=class_entry["nome"] if class_entry else None,
        )
        self.cargo_input = discord.ui.TextInput(
            label="Cargo do Discord", max_length=45,
            default=class_entry["cargo"] if class_entry else None,
        )
        self.arma_input = discord.ui.TextInput(
            label="Item ID ou nome da arma", required=False, max_length=60,
            default=(class_entry.get("item_id") or class_entry.get("arma_nome") or "") if class_entry else None,
            placeholder="Ex: T8_2H_HOLYSTAFF ou 'Cajado Sagrado'",
        )
        self.vagas_input = discord.ui.TextInput(
            label="Vagas (vazio = sem limite)", required=False, max_length=3,
            default=str(class_entry["vagas"]) if class_entry and class_entry.get("vagas") else None,
        )
        self.add_item(self.nome_input)
        self.add_item(self.cargo_input)
        self.add_item(self.arma_input)
        self.add_item(self.vagas_input)

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)
        classes = load_classes()
        arma_valor = str(self.arma_input.value).strip()
        item_id = arma_valor if arma_valor and _looks_like_item_id(arma_valor) else None
        arma_nome = arma_valor if arma_valor and not item_id else None
        vagas_valor = str(self.vagas_input.value).strip()
        vagas = int(vagas_valor) if vagas_valor.isdigit() else None

        if self.class_entry:
            alvo = next((c for c in classes if c["id"] == self.class_entry["id"]), None)
            if not alvo:
                await interaction.followup.send("Essa classe nao existe mais.", ephemeral=True)
                return
            novo_item = item_id or arma_nome or None
            if novo_item and novo_item != (alvo.get("item_id") or alvo.get("arma_nome")):
                alvo["emoji_id"] = None
            alvo["nome"] = str(self.nome_input.value)
            alvo["cargo"] = str(self.cargo_input.value)
            if arma_valor:
                alvo["item_id"] = item_id
                alvo["arma_nome"] = arma_nome
            alvo["vagas"] = vagas
            await ensure_class_emoji(alvo)
            save_classes(classes)
            await interaction.followup.send(f"✅ **{alvo['nome']}** atualizada.", ephemeral=True)
        else:
            if len(classes) >= 25:
                await interaction.followup.send("❌ Limite de 25 classes atingido.", ephemeral=True)
                return
            novo = {
                "id": _slug(str(self.nome_input.value)),
                "nome": str(self.nome_input.value),
                "item_id": item_id,
                "arma_nome": arma_nome,
                "cargo": str(self.cargo_input.value),
                "vagas": vagas,
                "ordem": len(classes) + 1,
                "role_id": None,
                "emoji_id": None,
            }
            await ensure_class_emoji(novo)
            classes.append(novo)
            save_classes(classes)
            await interaction.followup.send(f"✅ **{novo['nome']}** adicionada.", ephemeral=True)


class EditarClasseView(discord.ui.View):
    def __init__(self, class_entry):
        super().__init__(timeout=300)
        self.class_entry = class_entry

    @discord.ui.button(label="✏️ Editar", style=discord.ButtonStyle.primary)
    async def editar(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(ClasseModal(self.class_entry))

    @discord.ui.button(label="🗑️ Remover", style=discord.ButtonStyle.danger)
    async def remover(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        classes = load_classes()
        novas = [c for c in classes if c["id"] != self.class_entry["id"]]
        for i, c in enumerate(novas, start=1):
            c["ordem"] = i
        save_classes(novas)
        await interaction.response.edit_message(content=f"\U0001F5D1️ **{self.class_entry['nome']}** removida.", view=None)


class ClassesSelect(discord.ui.Select):
    def __init__(self):
        classes = load_classes()
        if classes:
            options = [
                discord.SelectOption(
                    label=c["nome"][:100],
                    value=c["id"],
                    description=(f"{c.get('item_id') or c.get('arma_nome') or 'sem arma definida'} · {c['cargo']}")[:100],
                    emoji=_partial_emoji_from_cache(c),
                )
                for c in classes[:25]
            ]
        else:
            options = [discord.SelectOption(label="(nenhuma classe cadastrada ainda)", value="_none")]
        super().__init__(placeholder="Escolha uma classe pra editar ou remover...", options=options, custom_id="classes_painel_select")

    async def callback(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        if self.values[0] == "_none":
            await interaction.response.send_message("Nenhuma classe cadastrada ainda — clique em ➕ Adicionar classe.", ephemeral=True)
            return
        classes = load_classes()
        c = next((x for x in classes if x["id"] == self.values[0]), None)
        if not c:
            await interaction.response.send_message("Classe nao encontrada (pode ter sido removida).", ephemeral=True)
            return
        detalhes = (
            f"**{c['nome']}**\n"
            f"Arma: {c.get('item_id') or c.get('arma_nome') or '—'}\n"
            f"Cargo: {c['cargo']}\n"
            f"Vagas: {c.get('vagas') or 'sem limite'}"
        )
        await interaction.response.send_message(detalhes, view=EditarClasseView(c), ephemeral=True)


class NovaClasseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="➕ Adicionar classe", style=discord.ButtonStyle.success, custom_id="classes_painel_nova", row=1)

    async def callback(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(ClasseModal())


class ClassesPainelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ClassesSelect())
        self.add_item(NovaClasseButton())


def _parse_classes_xlsx(raw_bytes):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
    ws = wb.active
    header_row = None
    for row in ws.iter_rows(min_row=1, max_row=15):
        values = [c.value for c in row]
        if values and values[0] == "Nome da Classe":
            header_row = row[0].row
            break
    if not header_row:
        raise ValueError("Nao achei a linha de cabecalho ('Nome da Classe'). Use o modelo original.")

    classes = []
    ordem = 1
    for row in ws.iter_rows(min_row=header_row + 1, max_row=header_row + 30):
        nome = row[0].value
        if not nome or str(nome).strip() == "" or "EXEMPLO" in str(nome).upper():
            continue
        item_id = row[1].value
        arma_nome = row[2].value
        cargo = row[3].value or f"{nome}"
        vagas = row[4].value
        classes.append({
            "id": _slug(str(nome)),
            "nome": str(nome).strip(),
            "item_id": str(item_id).strip() if item_id else None,
            "arma_nome": str(arma_nome).strip() if arma_nome else None,
            "cargo": str(cargo).strip().lstrip("@"),
            "vagas": int(vagas) if vagas not in (None, "") else None,
            "ordem": ordem,
            "role_id": None,
            "emoji_id": None,
        })
        ordem += 1
        if len(classes) >= 25:
            break
    return classes


@classes_group.command(name="importar", description="Importa o roster a partir da planilha classes_evento_modelo.xlsx preenchida")
@app_commands.describe(planilha="Arquivo .xlsx preenchido com o modelo")
@requer_pingar()
async def classes_importar(interaction: discord.Interaction, planilha: discord.Attachment):
    set_current_guild(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)
    if not planilha.filename.endswith(".xlsx"):
        await interaction.followup.send("Manda um arquivo .xlsx.", ephemeral=True)
        return
    raw = await planilha.read()
    try:
        novas_classes = _parse_classes_xlsx(raw)
    except Exception as e:
        await interaction.followup.send(f"Erro ao ler a planilha: {e}", ephemeral=True)
        return
    if not novas_classes:
        await interaction.followup.send("Nenhuma linha valida encontrada na planilha.", ephemeral=True)
        return

    antigas = {c["nome"].lower(): c for c in load_classes()}
    for c in novas_classes:
        antiga = antigas.get(c["nome"].lower())
        if antiga:
            c["role_id"] = antiga.get("role_id")
            if antiga.get("item_id") == c.get("item_id"):
                c["emoji_id"] = antiga.get("emoji_id")
        await ensure_class_emoji(c)

    save_classes(novas_classes)
    await interaction.followup.send(f"✅ Roster atualizado: {len(novas_classes)} classes importadas. Rode /ping-funcao de novo pra atualizar o painel.", ephemeral=True)


@classes_group.command(name="painel", description="Abre um painel interativo pra adicionar/editar/remover classes (sem decorar comando)")
@requer_pingar()
async def classes_painel(interaction: discord.Interaction):
    set_current_guild(interaction.guild_id)
    embed = discord.Embed(
        title="\U0001F6E0️ Painel de Classes/Builds",
        description="Escolha uma classe no menu pra editar ou remover, ou clique em ➕ pra adicionar uma nova.",
        color=GOLD,
    )
    await interaction.response.send_message(embed=embed, view=ClassesPainelView(), ephemeral=True)


async def templates_autocomplete(interaction: discord.Interaction, current: str):
    set_current_guild(interaction.guild_id)
    templates = load_templates()
    return [app_commands.Choice(name=t["nome"], value=t["id"]) for t in templates if current.lower() in t["nome"].lower()][:25]


async def slots_autocomplete(interaction: discord.Interaction, current: str):
    set_current_guild(interaction.guild_id)
    template_id = interaction.namespace.template
    templates = load_templates()
    tpl = next((t for t in templates if t["id"] == template_id), None)
    if not tpl:
        return []
    return [app_commands.Choice(name=s["nome"], value=s["id"]) for s in tpl["slots"] if current.lower() in s["nome"].lower()][:25]


# --- comp: composicoes com slots multi-build (swap), CRUD por comando + painel ---

@comp_group.command(name="listar", description="Lista as composicoes (templates) cadastradas e seus slots")
async def comp_listar(interaction: discord.Interaction):
    set_current_guild(interaction.guild_id)
    templates = load_templates()
    if not templates:
        await interaction.response.send_message("Nenhuma composicao cadastrada ainda.", ephemeral=True)
        return
    linhas = []
    for t in templates:
        linhas.append(f"{t['nome']} ({t['id']}) — {len(t['slots'])} slot(s)")
        for s in t["slots"]:
            builds_txt = " / ".join(b["nome"] for b in s["builds"])
            linhas.append(f"   - {s['nome']} (vagas: {s.get('vagas') or 'sem limite'}): {builds_txt}")
    texto = "\n".join(linhas)
    if len(texto) > 1900:
        texto = texto[:1900] + "\n... (truncado, use /comp painel pra ver tudo)"
    await interaction.response.send_message(f"```\n{texto}\n```", ephemeral=True)


@comp_group.command(name="criar", description="Cria uma composicao vazia (depois adicione slots com /comp slot_adicionar)")
@requer_pingar()
async def comp_criar(interaction: discord.Interaction, nome: str):
    set_current_guild(interaction.guild_id)
    templates = load_templates()
    novo_id = _slug(nome)
    if any(t["id"] == novo_id for t in templates):
        await interaction.response.send_message("Ja existe uma composicao com esse nome.", ephemeral=True)
        return
    templates.append({"id": novo_id, "nome": nome, "slots": []})
    save_templates(templates)
    await interaction.response.send_message(f"✅ Composicao **{nome}** criada. Use /comp slot_adicionar (ou /comp painel) pra montar os slots.", ephemeral=True)


@comp_group.command(name="remover", description="Remove uma composicao inteira")
@app_commands.autocomplete(template=templates_autocomplete)
@requer_pingar()
async def comp_remover(interaction: discord.Interaction, template: str):
    set_current_guild(interaction.guild_id)
    templates = load_templates()
    novas = [t for t in templates if t["id"] != template]
    if len(novas) == len(templates):
        await interaction.response.send_message("Composicao nao encontrada.", ephemeral=True)
        return
    save_templates(novas)
    await interaction.response.send_message("\U0001F5D1️ Composicao removida.", ephemeral=True)


@comp_group.command(name="slot_adicionar", description="Adiciona um slot a uma composicao (aceita 1+ builds, separadas por virgula)")
@app_commands.describe(
    template="Composicao",
    nome="Nome do slot (ex: Main Tank)",
    cargo="Cargo do Discord pra esse slot",
    builds="Builds separadas por virgula: Item ID ou nome da arma (ex: T8_2H_HALBERD, Martelo de Guerra)",
    vagas="Vagas nesse slot (vazio = sem limite)",
)
@app_commands.autocomplete(template=templates_autocomplete)
@requer_pingar()
async def comp_slot_adicionar(interaction: discord.Interaction, template: str, nome: str, cargo: str, builds: str, vagas: int = None):
    set_current_guild(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)
    templates = load_templates()
    tpl = next((t for t in templates if t["id"] == template), None)
    if not tpl:
        await interaction.followup.send("Composicao nao encontrada.", ephemeral=True)
        return
    builds_list = _parse_builds_texto(builds)
    if not builds_list:
        await interaction.followup.send("Informe pelo menos uma build.", ephemeral=True)
        return
    for b in builds_list:
        await ensure_class_emoji(b)
    novo_slot = {
        "id": _slug(nome), "nome": nome, "cargo": cargo,
        "vagas": vagas, "ordem": len(tpl["slots"]) + 1, "role_id": None,
        "item_id": builds_list[0].get("item_id"), "emoji_id": builds_list[0].get("emoji_id"),
        "builds": builds_list,
    }
    tpl["slots"].append(novo_slot)
    save_templates(templates)
    await interaction.followup.send(f"✅ Slot **{nome}** adicionado a **{tpl['nome']}** com {len(builds_list)} build(s).", ephemeral=True)


@comp_group.command(name="slot_remover", description="Remove um slot de uma composicao")
@app_commands.autocomplete(template=templates_autocomplete, slot=slots_autocomplete)
@requer_pingar()
async def comp_slot_remover(interaction: discord.Interaction, template: str, slot: str):
    set_current_guild(interaction.guild_id)
    templates = load_templates()
    tpl = next((t for t in templates if t["id"] == template), None)
    if not tpl:
        await interaction.response.send_message("Composicao nao encontrada.", ephemeral=True)
        return
    novos = [s for s in tpl["slots"] if s["id"] != slot]
    if len(novos) == len(tpl["slots"]):
        await interaction.response.send_message("Slot nao encontrado.", ephemeral=True)
        return
    tpl["slots"] = novos
    save_templates(templates)
    await interaction.response.send_message("\U0001F5D1️ Slot removido.", ephemeral=True)


class CompSlotModal(discord.ui.Modal, title="Novo slot"):
    nome_input = discord.ui.TextInput(label="Nome do slot (ex: Main Tank)", max_length=45)
    cargo_input = discord.ui.TextInput(label="Cargo do Discord", max_length=45)
    vagas_input = discord.ui.TextInput(label="Vagas (vazio = sem limite)", required=False, max_length=3)
    builds_input = discord.ui.TextInput(
        label="Builds (separe por virgula)", style=discord.TextStyle.paragraph,
        placeholder="T8_2H_HALBERD, Martelo de Guerra",
    )

    def __init__(self, template_id):
        super().__init__()
        self.template_id = template_id

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)
        templates = load_templates()
        tpl = next((t for t in templates if t["id"] == self.template_id), None)
        if not tpl:
            await interaction.followup.send("Composicao nao existe mais.", ephemeral=True)
            return
        builds_list = _parse_builds_texto(str(self.builds_input.value))
        if not builds_list:
            await interaction.followup.send("Informe pelo menos uma build.", ephemeral=True)
            return
        for b in builds_list:
            await ensure_class_emoji(b)
        vagas_val = str(self.vagas_input.value).strip()
        novo_slot = {
            "id": _slug(str(self.nome_input.value)), "nome": str(self.nome_input.value),
            "cargo": str(self.cargo_input.value),
            "vagas": int(vagas_val) if vagas_val.isdigit() else None,
            "ordem": len(tpl["slots"]) + 1, "role_id": None,
            "item_id": builds_list[0].get("item_id"), "emoji_id": builds_list[0].get("emoji_id"),
            "builds": builds_list,
        }
        tpl["slots"].append(novo_slot)
        save_templates(templates)
        await interaction.followup.send(f"✅ Slot **{novo_slot['nome']}** adicionado.", ephemeral=True)


class SlotRemoveSelect(discord.ui.Select):
    def __init__(self, tpl):
        options = [discord.SelectOption(label=s["nome"][:100], value=s["id"]) for s in tpl["slots"][:25]] or [discord.SelectOption(label="(sem slots ainda)", value="_none")]
        super().__init__(placeholder="Remover um slot...", options=options)
        self.template_id = tpl["id"]

    async def callback(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        if self.values[0] == "_none":
            await interaction.response.send_message("Essa composicao nao tem slots ainda — clique em ➕ Adicionar slot.", ephemeral=True)
            return
        templates = load_templates()
        tpl = next((t for t in templates if t["id"] == self.template_id), None)
        if not tpl:
            await interaction.response.send_message("Composicao nao existe mais.", ephemeral=True)
            return
        tpl["slots"] = [s for s in tpl["slots"] if s["id"] != self.values[0]]
        save_templates(templates)
        await interaction.response.edit_message(content="\U0001F5D1️ Slot removido.", view=None, embed=None)


class CompDetailView(discord.ui.View):
    def __init__(self, tpl):
        super().__init__(timeout=300)
        self.template_id = tpl["id"]
        self.add_item(SlotRemoveSelect(tpl))

    @discord.ui.button(label="➕ Adicionar slot", style=discord.ButtonStyle.success, row=1)
    async def add_slot(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(CompSlotModal(self.template_id))


class TemplateSelect(discord.ui.Select):
    def __init__(self):
        templates = load_templates()
        if templates:
            options = [discord.SelectOption(label=t["nome"][:100], value=t["id"], description=f"{len(t['slots'])} slot(s)") for t in templates[:25]]
        else:
            options = [discord.SelectOption(label="(nenhuma composicao ainda)", value="_none")]
        super().__init__(placeholder="Escolha uma composicao pra ver/editar...", options=options, custom_id="comp_painel_select")

    async def callback(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        if self.values[0] == "_none":
            await interaction.response.send_message("Nenhuma composicao cadastrada ainda — clique em ➕ Nova composicao.", ephemeral=True)
            return
        templates = load_templates()
        tpl = next((t for t in templates if t["id"] == self.values[0]), None)
        if not tpl:
            await interaction.response.send_message("Composicao nao encontrada.", ephemeral=True)
            return
        linhas = [f"**{s['nome']}** (vagas: {s.get('vagas') or 'sem limite'}): {' / '.join(b['nome'] for b in s['builds'])}" for s in tpl["slots"]] or ["(sem slots ainda)"]
        await interaction.response.send_message(f"**{tpl['nome']}**\n" + "\n".join(linhas), view=CompDetailView(tpl), ephemeral=True)


class NovaCompModal(discord.ui.Modal, title="Nova composicao"):
    nome_input = discord.ui.TextInput(label="Nome da composicao (ex: ZvZ Padrao)", max_length=45)

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)
        templates = load_templates()
        novo_id = _slug(str(self.nome_input.value))
        if any(t["id"] == novo_id for t in templates):
            await interaction.followup.send("Ja existe uma composicao com esse nome.", ephemeral=True)
            return
        templates.append({"id": novo_id, "nome": str(self.nome_input.value), "slots": []})
        save_templates(templates)
        await interaction.followup.send(f"✅ Composicao **{self.nome_input.value}** criada. Abra o painel de novo pra adicionar slots.", ephemeral=True)


class NovaCompButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="➕ Nova composicao", style=discord.ButtonStyle.success, custom_id="comp_painel_nova", row=1)

    async def callback(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(NovaCompModal())


class CompPainelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TemplateSelect())
        self.add_item(NovaCompButton())


@comp_group.command(name="painel", description="Painel interativo pra criar/editar composicoes e seus slots")
@requer_pingar()
async def comp_painel(interaction: discord.Interaction):
    set_current_guild(interaction.guild_id)
    embed = discord.Embed(
        title="\U0001F9E9 Painel de Composicoes",
        description="Escolha uma composicao pra ver/editar os slots, ou clique em ➕ pra criar uma nova.",
        color=GOLD,
    )
    await interaction.response.send_message(embed=embed, view=CompPainelView(), ephemeral=True)


async def eventos_abertos_autocomplete(interaction: discord.Interaction, current: str):
    set_current_guild(interaction.guild_id)
    events = load_events()
    abertos = [(eid, e) for eid, e in events.items() if e["status"] == "aberto"]
    return [app_commands.Choice(name=e["nome"], value=eid) for eid, e in abertos if current.lower() in e["nome"].lower()][:25]


@bot.tree.command(name="ping-funcao", description="Publica o painel de funcoes (clique pra ser marcado e notificado)")
@requer_pingar()
async def ping_funcao(interaction: discord.Interaction):
    set_current_guild(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)
    classes = load_classes()
    for c in classes:
        await ensure_class_emoji(c)
    save_classes(classes)

    embed = discord.Embed(
        title="\U0001F3AF Ping de Funcao — STOMP",
        description="Clique no botao da sua funcao pra ser marcado e notificado quando ela for chamada. Clique de novo pra remover.",
        color=GOLD,
    )
    embed.set_footer(text="Bot criado por Jeverson")
    view = FuncaoPingView(classes)
    msg = await interaction.channel.send(embed=embed, view=view)
    bot.add_view(view)
    await interaction.followup.send(f"✅ Painel publicado em {msg.jump_url}", ephemeral=True)


# =========================
# PING DE CONTEUDO (topico + imagem da build + vagas por reacao)
# =========================

def _ping_copy(titulo, autor):
    return (
        f"\U0001F4E2 {titulo} Está Convocando você! Chamado por {autor.mention}. "
        f"Clique no botão da sua função aqui embaixo, é por ordem de chegada, "
        f"então corre antes que lote! Bora fechar esse grupo! \U0001F525"
    )


def _match_build_ref(nome_funcao, comp):
    """Acha, pelo nome da funcao digitada no modal, o slot correspondente na
    composicao de referencia e devolve o nome da arma/build daquele slot (pra
    mostrar como sub-linha, tipo 'Martelo Estrondoso' embaixo do cargo)."""
    alvo = _strip_accents(nome_funcao).lower()
    for slot in (comp.get("slots") or []):
        if _strip_accents(slot["nome"]).lower() == alvo:
            builds = slot.get("builds") or []
            if builds:
                b = builds[0]
                return b.get("arma_nome") or b.get("nome")
    return None


_ITEM_NAME_INDEX = None


def _item_name_index():
    """Indice {item_id: nome} montado a partir do items_db.json (catalogo
    usado no seletor visual do site). Cacheado em memoria."""
    global _ITEM_NAME_INDEX
    if _ITEM_NAME_INDEX is not None:
        return _ITEM_NAME_INDEX
    idx = {}
    for lista in load_items_db().values():
        for entry in lista:
            if entry.get("id"):
                idx[entry["id"]] = entry.get("nome") or entry["id"]
    _ITEM_NAME_INDEX = idx
    return idx


def _item_name(item_id):
    if not item_id:
        return None
    return _item_name_index().get(item_id, item_id)


def _get_build_for_funcao(ping, funcao):
    """Acha a build completa (equip, nao so o nome) que corresponde a funcao
    de um ping, pra poder montar a mensagem privada quando alguem confirma."""
    templates = load_templates(ping.get("guild_id"))
    comp = next((t for t in templates if t["id"] == ping.get("composicao_id")), None)
    if not comp:
        return None
    alvo = _strip_accents(funcao["nome"]).lower()
    for slot in (comp.get("slots") or []):
        if _strip_accents(slot["nome"]).lower() == alvo:
            builds = slot.get("builds") or []
            if builds:
                return builds[0]
    return None


_DM_SLOT_LABELS = [
    ("mainhand", "Arma"),
    ("head", "Superior"),
    ("chest", "Peito"),
    ("shoes", "Inferior"),
    ("cape", "Capa"),
    ("offhand", "Off-Hand"),
    ("food", "Comida"),
    ("potion", "Poção"),
]


def _build_dm_message(ping, funcao, build, thread_url=None):
    """Monta o texto + imagem (tira de icones) que vao no privado de quem
    confirmou a funcao, no mesmo espirito do card 'Sua build' de referencia."""
    equip = get_build_equip(build)
    linhas = [
        f"🛡️ **{ping.get('titulo') or 'Ping'}**",
        f"🛡️ Sua build — **{funcao['nome']}** · {build.get('nome') or build.get('arma_nome') or '?'}",
        "",
        "🛡️ **Set**",
    ]
    algum_item = False
    for key, label in _DM_SLOT_LABELS:
        item_id = equip.get(key, {}).get("item")
        if not item_id:
            continue
        algum_item = True
        linhas.append(f"**{label}:** {_item_name(item_id)}")
        info_hab = get_item_abilities(item_id)
        if info_hab:
            linhas.append(" · ".join(f"`{h['slot']}` {h['nome']}" for h in info_hab["habilidades"]))
        linhas.append("")

    if not algum_item and build.get("item_id"):
        linhas.append(f"**Arma:** {_item_name(build['item_id'])}")
        info_hab = get_item_abilities(build["item_id"])
        if info_hab:
            linhas.append(" · ".join(f"`{h['slot']}` {h['nome']}" for h in info_hab["habilidades"]))
        linhas.append("")

    if thread_url:
        linhas.append(f"🔗 [Abrir o ping]({thread_url})")

    content = "\n".join(linhas).strip()

    file = None
    try:
        detalhe_bytes = render_build_detail_card(funcao["nome"], build)
        file = discord.File(io.BytesIO(detalhe_bytes), filename="build_detalhe.png")
    except Exception:
        pass

    return content, file


async def _dm_build_para_funcao(interaction, ping, funcao):
    """Tenta achar a build da funcao e mandar no privado de quem confirmou.
    Nunca derruba o fluxo do ping - se o usuario tiver DM fechada ou der
    qualquer erro, so ignora silenciosamente."""
    build = _get_build_for_funcao(ping, funcao)
    if not build:
        return
    try:
        thread_url = interaction.channel.jump_url if interaction.channel else None
        content, file = await asyncio.to_thread(_build_dm_message, ping, funcao, build, thread_url)
        kwargs = {"content": content}
        if file:
            kwargs["file"] = file
        await interaction.user.send(**kwargs)
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"[aviso] falha ao mandar build no privado de {interaction.user.id}: {e}")


def _build_ping_embed(ping):
    """Embed do painel do ping, em colunas por funcao (igual o visual de
    referencia que o usuario mandou): 1a prioridade numerada, depois 2a
    prioridade (transbordo automatico de quem reagiu depois da vaga cheia),
    Banco (reserva sem limite) e Ausentes (com motivo). Numeracao e continua
    do inicio da 1a prioridade ate o fim da 2a."""
    funcoes = ping["funcoes"]
    total_confirmados = sum(len(f["claimed"]) + len(f.get("extra") or []) for f in funcoes)
    total_vagas = sum(f["vagas"] for f in funcoes)

    topo = [f"`ID: {ping.get('ping_id') or 'legado'}`"]
    if ping.get("local"):
        topo.append(f"\U0001F4CD {ping['local']}")
    if ping.get("fee"):
        topo.append(f"\U0001F4B0 FEE: {ping['fee']}")
    if ping.get("quando"):
        ts = int(ping["quando"])
        topo.append(f"⏰ <t:{ts}:t> · <t:{ts}:R>")
    else:
        topo.append("\U0001F534 Ao vivo agora")
    topo.append(f"\U0001F465 {total_confirmados}/{total_vagas}")
    if ping.get("canal_voz_id") and ping.get("contagem_iniciada"):
        topo.append("\U0001F50A Sala de voz ativa — contando tempo de call pra dividir o loot proporcional no final")
    elif ping.get("canal_voz_id"):
        topo.append("\U0001F50A Sala de voz criada")

    embed = discord.Embed(title=ping["titulo"], description="\n".join(topo), color=GOLD)

    contador = [0]

    def _linhas(uids):
        linhas = []
        for uid in uids:
            contador[0] += 1
            linhas.append(f"`{contador[0]:>2}` <@{uid}>")
        return "\n".join(linhas) or "—"

    pt1_fields = []
    for f in funcoes:
        preenchido = len(f["claimed"])
        total = f["vagas"]
        status = " ✅" if preenchido >= total else ""
        valor = _linhas(f["claimed"])
        if f.get("build_ref"):
            valor += f"\n> {f['build_ref']}"
        pt1_fields.append((f"{f['emoji']} {f['nome']} ({preenchido}/{total}){status}", valor))

    pt2_fields = []
    for f in funcoes:
        extra = f.get("extra") or []
        if extra:
            pt2_fields.append((f"{f['emoji']} {f['nome']} (+{len(extra)} extra)", _linhas(extra)))

    MAX_ROLE_FIELDS = 20  # deixa folga pros campos de Banco/Ausentes (limite do Discord e 25 campos)
    adicionados = 0

    def _add_field(name, value, inline):
        nonlocal adicionados
        if adicionados >= MAX_ROLE_FIELDS:
            return False
        embed.add_field(name=name, value=value[:1024] if value else "​", inline=inline)
        adicionados += 1
        return True

    if pt1_fields:
        _add_field("⭐ 1ª Prioridade", "​", False)
        for nome, valor in pt1_fields:
            if not _add_field(nome, valor, True):
                break

    if pt2_fields:
        if _add_field("⭐ 2ª Prioridade (extra)", "​", False):
            for nome, valor in pt2_fields:
                if not _add_field(nome, valor, True):
                    break

    banco = ping.get("banco") or []
    if banco and len(embed.fields) < 25:
        embed.add_field(name=f"\U0001F3E6 Banco ({len(banco)})", value=(" ".join(f"<@{uid}>" for uid in banco))[:1024], inline=False)

    ausentes = ping.get("ausentes") or []
    if ausentes and len(embed.fields) < 25:
        linhas_aus = []
        for a in ausentes:
            motivo = f" — {a['motivo']}" if a.get("motivo") else ""
            linhas_aus.append(f"<@{a['uid']}>{motivo}")
        embed.add_field(name=f"❌ Ausentes ({len(ausentes)})", value="\n".join(linhas_aus)[:1024], inline=False)

    embed.set_footer(text=f"Criado por {ping.get('criado_por_nome') or '?'}")
    return embed


class PingFuncaoView(discord.ui.View):
    """Um botao por funcao (marca/desmarca vaga) + botao Sair. custom_id embute
    o ping_id, entao sobrevive a restart do bot (re-registrado em on_ready)."""

    def __init__(self, ping_id, funcoes):
        super().__init__(timeout=None)
        for idx, f in enumerate(funcoes[:20]):
            preenchido = len(f["claimed"])
            total = f["vagas"]
            full = preenchido >= total
            btn = discord.ui.Button(
                label=f"{f['nome']} ({preenchido}/{total})"[:80],
                style=discord.ButtonStyle.success if full else discord.ButtonStyle.secondary,
                custom_id=f"pingfn:{ping_id}:{idx}",
                emoji=f["emoji"],
                row=idx // 5,
            )
            btn.callback = self._make_callback(ping_id, idx)
            self.add_item(btn)

        sair_btn = discord.ui.Button(label="Sair", style=discord.ButtonStyle.danger, custom_id=f"pingsair:{ping_id}", row=4)
        sair_btn.callback = self._make_sair_callback(ping_id)
        self.add_item(sair_btn)

        config_btn = discord.ui.Button(label="Config", style=discord.ButtonStyle.secondary, custom_id=f"pingcfg:{ping_id}", row=4)
        config_btn.callback = self._make_config_callback(ping_id)
        self.add_item(config_btn)

        evento_btn = discord.ui.Button(
            label="\U0001F3C1 Criar evento", style=discord.ButtonStyle.primary, custom_id=f"pingevt:{ping_id}", row=4,
        )
        evento_btn.callback = self._make_evento_callback(ping_id)
        self.add_item(evento_btn)

        banco_btn = discord.ui.Button(label="\U0001F3E6 Banco", style=discord.ButtonStyle.secondary, custom_id=f"pingbanco:{ping_id}", row=4)
        banco_btn.callback = self._make_banco_callback(ping_id)
        self.add_item(banco_btn)

        ausencia_btn = discord.ui.Button(label="❌ Ausência", style=discord.ButtonStyle.secondary, custom_id=f"pingausencia:{ping_id}", row=4)
        ausencia_btn.callback = self._make_ausencia_callback(ping_id)
        self.add_item(ausencia_btn)

    def _make_callback(self, ping_id, idx):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            await handle_ping_button(interaction, ping_id, idx)
        return callback

    def _make_sair_callback(self, ping_id):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            await handle_ping_sair(interaction, ping_id)
        return callback

    def _make_config_callback(self, ping_id):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            await handle_ping_config(interaction, ping_id)
        return callback

    def _make_evento_callback(self, ping_id):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            await handle_criar_evento_button(interaction, ping_id)
        return callback

    def _make_banco_callback(self, ping_id):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            await handle_ping_banco(interaction, ping_id)
        return callback

    def _make_ausencia_callback(self, ping_id):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            await handle_ping_ausencia(interaction, ping_id)
        return callback


async def handle_ping_button(interaction: discord.Interaction, ping_id, idx):
    pings = load_pings()
    ping = pings.get(ping_id)
    if not ping or ping.get("encerrado"):
        await interaction.response.send_message("Esse ping já foi encerrado.", ephemeral=True)
        return
    funcoes = ping["funcoes"]
    if idx >= len(funcoes):
        await interaction.response.send_message("❌ Função inválida.", ephemeral=True)
        return
    for f in funcoes:
        f.setdefault("extra", [])
    ping.setdefault("banco", [])
    ping.setdefault("ausentes", [])
    funcao = funcoes[idx]
    uid = interaction.user.id

    # clicar de novo na mesma funcao onde ja esta (1a ou 2a prioridade) libera a vaga
    if uid in funcao["claimed"] or uid in funcao["extra"]:
        era_extra = uid in funcao["extra"]
        (funcao["extra"] if era_extra else funcao["claimed"]).remove(uid)
        pings[ping_id] = ping
        save_pings(pings)
        await interaction.response.edit_message(embed=_build_ping_embed(ping), view=PingFuncaoView(ping_id, funcoes))
        try:
            await interaction.channel.send(f"\U0001F513 {interaction.user.mention} liberou a vaga de **{funcao['nome']}**. ({len(funcao['claimed'])}/{funcao['vagas']})")
        except Exception:
            pass
        return

    # tira de qualquer outra funcao, do Banco ou dos Ausentes antes de entrar
    # nessa - cada pessoa so ocupa um lugar por vez no ping
    funcao_anterior = None
    for outro in funcoes:
        if outro is not funcao and uid in outro["claimed"]:
            outro["claimed"].remove(uid)
            funcao_anterior = outro
            break
        if outro is not funcao and uid in outro["extra"]:
            outro["extra"].remove(uid)
            funcao_anterior = outro
            break
    if uid in ping["banco"]:
        ping["banco"].remove(uid)
    ping["ausentes"] = [a for a in ping["ausentes"] if a["uid"] != uid]

    # 1a prioridade enquanto tiver vaga; depois disso transborda sozinho pra
    # 2a prioridade (extra), sem limite - ninguem e rejeitado
    vai_pra_extra = len(funcao["claimed"]) >= funcao["vagas"]
    (funcao["extra"] if vai_pra_extra else funcao["claimed"]).append(uid)
    pings[ping_id] = ping
    save_pings(pings)
    await interaction.response.edit_message(embed=_build_ping_embed(ping), view=PingFuncaoView(ping_id, funcoes))
    try:
        tier_txt = " (2ª prioridade - extra)" if vai_pra_extra else ""
        if funcao_anterior:
            await interaction.channel.send(
                f"\U0001F504 {interaction.user.mention} saiu de **{funcao_anterior['nome']}** e entrou em "
                f"**{funcao['nome']}**{tier_txt}! {funcao['emoji']} ({len(funcao['claimed'])}/{funcao['vagas']})"
            )
        else:
            await interaction.channel.send(f"{funcao['emoji']} {interaction.user.mention} confirmado como **{funcao['nome']}**{tier_txt}! ({len(funcao['claimed'])}/{funcao['vagas']})")
    except Exception:
        pass

    # manda a build dessa funcao no privado de quem acabou de confirmar (vale pra 1a e 2a prioridade)
    await _dm_build_para_funcao(interaction, ping, funcao)


async def handle_ping_sair(interaction: discord.Interaction, ping_id):
    pings = load_pings()
    ping = pings.get(ping_id)
    if not ping or ping.get("encerrado"):
        await interaction.response.send_message("Esse ping já foi encerrado.", ephemeral=True)
        return
    uid = interaction.user.id
    funcao_saida = None
    for f in ping["funcoes"]:
        f.setdefault("extra", [])
        if uid in f["claimed"]:
            f["claimed"].remove(uid)
            funcao_saida = f
            break
        if uid in f["extra"]:
            f["extra"].remove(uid)
            funcao_saida = f
            break
    ping.setdefault("banco", [])
    estava_banco = uid in ping["banco"]
    if estava_banco:
        ping["banco"].remove(uid)
    ping.setdefault("ausentes", [])
    tinha_ausencia = any(a["uid"] == uid for a in ping["ausentes"])
    ping["ausentes"] = [a for a in ping["ausentes"] if a["uid"] != uid]

    if not funcao_saida and not estava_banco and not tinha_ausencia:
        await interaction.response.send_message("Você não está marcado em nada nesse ping.", ephemeral=True)
        return
    pings[ping_id] = ping
    save_pings(pings)
    await interaction.response.edit_message(embed=_build_ping_embed(ping), view=PingFuncaoView(ping_id, ping["funcoes"]))
    try:
        if funcao_saida:
            motivo_saida = f"estava em **{funcao_saida['nome']}**"
        elif estava_banco:
            motivo_saida = "estava no Banco"
        else:
            motivo_saida = "estava marcado como ausente"
        await interaction.channel.send(f"\U0001F6AA {interaction.user.mention} saiu do ping ({motivo_saida}).")
    except Exception:
        pass


async def handle_ping_banco(interaction: discord.Interaction, ping_id):
    pings = load_pings()
    ping = pings.get(ping_id)
    if not ping or ping.get("encerrado"):
        await interaction.response.send_message("Esse ping já foi encerrado.", ephemeral=True)
        return
    ping.setdefault("banco", [])
    uid = interaction.user.id

    if uid in ping["banco"]:
        ping["banco"].remove(uid)
        pings[ping_id] = ping
        save_pings(pings)
        await interaction.response.edit_message(embed=_build_ping_embed(ping), view=PingFuncaoView(ping_id, ping["funcoes"]))
        try:
            await interaction.channel.send(f"\U0001F3E6 {interaction.user.mention} saiu do Banco.")
        except Exception:
            pass
        return

    # tira de qualquer funcao (1a ou 2a prioridade) e dos ausentes antes de
    # entrar no Banco - cada pessoa so ocupa um lugar por vez no ping
    for f in ping["funcoes"]:
        f.setdefault("extra", [])
        if uid in f["claimed"]:
            f["claimed"].remove(uid)
        if uid in f["extra"]:
            f["extra"].remove(uid)
    ping.setdefault("ausentes", [])
    ping["ausentes"] = [a for a in ping["ausentes"] if a["uid"] != uid]

    ping["banco"].append(uid)
    pings[ping_id] = ping
    save_pings(pings)
    await interaction.response.edit_message(embed=_build_ping_embed(ping), view=PingFuncaoView(ping_id, ping["funcoes"]))
    try:
        await interaction.channel.send(f"\U0001F3E6 {interaction.user.mention} entrou no Banco (reserva, sem função fixa).")
    except Exception:
        pass


class PingAusenciaModal(discord.ui.Modal, title="Marcar ausência"):
    motivo = discord.ui.TextInput(
        label="Motivo (opcional)", style=discord.TextStyle.paragraph, required=False, max_length=200,
        placeholder="Ex: fazendo relatórios do trampo, se conseguir entrar eu colo",
    )

    def __init__(self, ping_id):
        super().__init__()
        self.ping_id = ping_id

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)
        pings = load_pings()
        ping = pings.get(self.ping_id)
        if not ping or ping.get("encerrado"):
            await interaction.followup.send("Esse ping já foi encerrado.", ephemeral=True)
            return
        uid = interaction.user.id

        for f in ping["funcoes"]:
            f.setdefault("extra", [])
            if uid in f["claimed"]:
                f["claimed"].remove(uid)
            if uid in f["extra"]:
                f["extra"].remove(uid)
        ping.setdefault("banco", [])
        if uid in ping["banco"]:
            ping["banco"].remove(uid)
        ping.setdefault("ausentes", [])
        ping["ausentes"] = [a for a in ping["ausentes"] if a["uid"] != uid]
        ping["ausentes"].append({"uid": uid, "motivo": str(self.motivo.value).strip()})

        pings[self.ping_id] = ping
        save_pings(pings)

        try:
            if interaction.channel and ping.get("mensagem_id"):
                painel_msg = await interaction.channel.fetch_message(ping["mensagem_id"])
                await painel_msg.edit(embed=_build_ping_embed(ping), view=PingFuncaoView(self.ping_id, ping["funcoes"]))
        except discord.HTTPException:
            pass
        await interaction.followup.send("❌ Marcado(a) como ausente nesse ping.", ephemeral=True)


async def handle_ping_ausencia(interaction: discord.Interaction, ping_id):
    pings = load_pings()
    ping = pings.get(ping_id)
    if not ping or ping.get("encerrado"):
        await interaction.response.send_message("Esse ping já foi encerrado.", ephemeral=True)
        return
    await interaction.response.send_modal(PingAusenciaModal(ping_id))


async def handle_ping_config(interaction: discord.Interaction, ping_id):
    pings = load_pings()
    ping = pings.get(ping_id)
    if not ping:
        await interaction.response.send_message("❌ Ping não encontrado.", ephemeral=True)
        return
    pode_encerrar = (interaction.user.id == ping.get("criado_por")) or _pode_pingar(interaction.user, interaction.guild)
    sala_txt = "—"
    if ping.get("canal_voz_id"):
        voice_channel = interaction.guild.get_channel(ping["canal_voz_id"]) if interaction.guild else None
        sala_txt = voice_channel.mention if voice_channel else "(sala removida)"
    info = (
        f"**ID:** `{ping.get('ping_id') or 'legado'}`\n"
        f"**Local:** {ping.get('local') or '—'}\n"
        f"**Fee:** {ping.get('fee') or '—'}\n"
        f"**Criado por:** {ping.get('criado_por_nome') or '?'}\n"
        f"**Sala de voz / contagem de tempo:** {sala_txt}\n"
        f"**Status:** {'Encerrado' if ping.get('encerrado') else 'Aberto'}"
    )
    view = discord.ui.View(timeout=60)
    if pode_encerrar and not ping.get("encerrado"):
        if not ping.get("canal_voz_id"):
            btn_sala = discord.ui.Button(label="\U0001F50A Iniciar sala (contar tempo p/ split)", style=discord.ButtonStyle.primary)

            async def _sala_cb(inter2: discord.Interaction):
                set_current_guild(inter2.guild_id)
                await inter2.response.defer(ephemeral=True, thinking=True)
                ok, msg = await _iniciar_sala_ping_core(ping_id, inter2.user, inter2.guild)
                await inter2.followup.send(msg, ephemeral=True)
            btn_sala.callback = _sala_cb
            view.add_item(btn_sala)

        btn = discord.ui.Button(label="Encerrar ping", style=discord.ButtonStyle.danger)

        async def _encerrar_cb(inter2: discord.Interaction):
            set_current_guild(inter2.guild_id)
            await _encerrar_ping_core(inter2, ping_id)
        btn.callback = _encerrar_cb
        view.add_item(btn)
    await interaction.response.send_message(info, view=view, ephemeral=True)


async def _iniciar_sala_ping_core(ping_key, user, guild):
    """Cria uma sala de voz pro proprio ping e comeca a contar o tempo de
    quem ja confirmou uma funcao (1a e 2a prioridade) - pro ping poder fazer
    um split proporcional ao tempo de call no final (/encerrar-ping), igual
    o evento, sem precisar converter o ping num evento de verdade. Devolve
    (ok: bool, mensagem: str)."""
    pings = load_pings()
    ping = pings.get(ping_key)
    if not ping:
        return False, "❌ Não achei esse ping."
    if ping.get("encerrado"):
        return False, "❌ Esse ping já foi encerrado."
    if not guild:
        return False, "❌ Isso só funciona dentro de um servidor."

    pode = (user.id == ping.get("criado_por")) or _pode_pingar(user, guild)
    if not pode:
        return False, "❌ Só quem criou o ping, um caller, um gestor de saldo ou um admin do servidor pode iniciar a sala."

    ping.setdefault("participantes_tempo", {})
    if ping.get("canal_voz_id"):
        voice_channel = guild.get_channel(ping["canal_voz_id"])
        if voice_channel:
            return False, f"⚠️ Esse ping já tem uma sala ativa: {voice_channel.mention}."

    category = discord.utils.get(guild.categories, name=EVENTOS_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(EVENTOS_CATEGORY_NAME)
    voice_channel = await guild.create_voice_channel(f"\U0001F50A {ping['titulo']}"[:100], category=category)

    # junta 1a e 2a prioridade (claimed + extra) - quem esta so no Banco ou
    # Ausente fica de fora, por nao ter funcao definida (mesmo criterio da
    # ponte ping->evento)
    for f in ping["funcoes"]:
        f.setdefault("extra", [])
        for uid in list(f["claimed"]) + list(f["extra"]):
            ping["participantes_tempo"].setdefault(str(uid), {"entrou_em": None, "tempo_total": 0.0})
            membro = guild.get_member(uid)
            if membro and membro.voice and membro.voice.channel and membro.voice.channel.id != voice_channel.id:
                try:
                    await membro.move_to(voice_channel, reason="Sala do ping iniciada")
                except Exception:
                    pass

    agora = time.time()
    ids_na_call = {str(m.id) for m in voice_channel.members}
    qtd_contando = 0
    for uid, p in ping["participantes_tempo"].items():
        if uid in ids_na_call and not p.get("entrou_em"):
            p["entrou_em"] = agora
            qtd_contando += 1

    ping["canal_voz_id"] = voice_channel.id
    ping["contagem_iniciada"] = True
    pings[ping_key] = ping
    save_pings(pings)

    canal_painel = guild.get_channel_or_thread(ping["thread_id"]) if ping.get("thread_id") else guild.get_channel(ping["canal_id"])
    if canal_painel:
        await _refresh_ping_message(canal_painel, ping)

    return True, (
        f"\U0001F50A **Sala do ping iniciada!** Call: {voice_channel.mention}. "
        f"{qtd_contando} participante(s) já na call começam a contar tempo agora, todo mundo junto. "
        "Quem entrar depois conta a partir do próprio ingresso. Quando encerrar com `/encerrar-ping`, "
        "a divisão de loot vai ser proporcional ao tempo de call de cada um."
    )


class PingSlotPickerSelect(discord.ui.Select):
    """Select com as funcoes ja cadastradas na composicao (estilo 'Pick a role...'),
    pra nao precisar digitar a lista de vagas na mao - escolhe quais funcoes do
    roster da composicao entram nesse ping. Cada funcao selecionada entra com
    1 vaga - a quantidade so sobe se a mesma funcao aparecer repetida no
    campo de texto depois (mesma regra de sempre: 1 nome = 1 vaga, 2 nomes
    iguais = 2 vagas - nao herda a quantidade configurada na composicao)."""

    def __init__(self, canal, comp):
        slots = comp.get("slots") or []
        options = [discord.SelectOption(label=s["nome"][:100], value=s["id"]) for s in slots[:25]]
        super().__init__(
            placeholder="Selecionar funcoes da composicao...",
            min_values=1, max_values=len(options), options=options,
        )
        self.canal = canal
        self.comp = comp

    async def callback(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        # mantem a ordem da composicao (nao a ordem que foi clicado no
        # select) - 1 linha por funcao selecionada, sempre 1 vaga; pra mais
        # de 1 vaga da mesma funcao, repete o nome manualmente no campo
        # depois (o campo continua editavel)
        linhas = [slot["nome"] for slot in (self.comp.get("slots") or []) if slot["id"] in self.values]
        prefill = "\n".join(linhas)
        await interaction.response.send_modal(PingConteudoModal(self.canal, self.comp, vagas_prefill=prefill))


class PingSlotPickerView(discord.ui.View):
    """Tela intermediaria do /ping-conteudo: escolher as funcoes pelo select
    (preenche a vaga automatico) ou ir direto pro campo de texto livre - as
    duas formas continuam preenchendo o mesmo campo, que pode ser editado."""

    def __init__(self, canal, comp):
        super().__init__(timeout=180)
        self.canal = canal
        self.comp = comp
        if comp.get("slots"):
            self.add_item(PingSlotPickerSelect(canal, comp))

    @discord.ui.button(label="✏️ Escrever vagas na mão", style=discord.ButtonStyle.secondary, row=1)
    async def escrever_na_mao(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(PingConteudoModal(self.canal, self.comp))


class PingConteudoModal(discord.ui.Modal, title="Criar ping de conteudo"):
    titulo_input = discord.ui.TextInput(label="Titulo do ping", placeholder="Ex: Gank Noturno", required=False, max_length=80)
    local_input = discord.ui.TextInput(label="Local", placeholder="Ex: HO, Caminho da Fé", required=False, max_length=60)
    fee_input = discord.ui.TextInput(label="Fee", placeholder="Grátis ou ex: 5%", required=False, max_length=40, default="Grátis")
    horario_input = discord.ui.TextInput(
        label="Horário (DD/MM HH:MM, vazio = ao vivo)", placeholder="17/08 21:00", required=False, max_length=20,
    )
    vagas_input = discord.ui.TextInput(
        label="Vagas (uma funcao por linha)", style=discord.TextStyle.paragraph,
        placeholder="Tank\nTank\nHealer pt\nSuporte\nDPS\nDPS", max_length=800,
    )

    def __init__(self, canal, comp, vagas_prefill=None):
        super().__init__()
        self.canal = canal
        self.comp = comp
        # vindo do select de funcoes da composicao (PingSlotPickerSelect) - so
        # pre-preenche, continua editavel igual quando digitado na mao
        if vagas_prefill:
            self.vagas_input.default = vagas_prefill[:800]

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)

        funcoes = _parse_slots_texto(str(self.vagas_input.value))
        if not funcoes:
            await interaction.followup.send("❌ Não entendi nenhuma vaga — escreva uma função por linha (ex: Tank, Tank, DPS).", ephemeral=True)
            return
        funcoes = _assign_emojis(funcoes)
        for f in funcoes:
            f["claimed"] = []
            f["extra"] = []
            f["build_ref"] = _match_build_ref(f["nome"], self.comp) if self.comp else None

        titulo = str(self.titulo_input.value).strip() or (self.comp["nome"] if self.comp else "Ping de conteúdo")
        local = str(self.local_input.value).strip()
        fee = str(self.fee_input.value).strip()

        quando = None
        horario_raw = str(self.horario_input.value).strip()
        if horario_raw:
            partes = horario_raw.split()
            if len(partes) != 2:
                await interaction.followup.send("❌ Horário inválido — use o formato `DD/MM HH:MM` (ex: 17/08 21:00).", ephemeral=True)
                return
            try:
                dt = _parse_data_hora(partes[0], partes[1])
            except (ValueError, IndexError):
                await interaction.followup.send("❌ Horário inválido — use o formato `DD/MM HH:MM` (ex: 17/08 21:00).", ephemeral=True)
                return
            quando = dt.timestamp()

        # imagem da build so existe se escolheu uma composicao - com composicao
        # opcional, um ping so com a lista de vagas (sem referencia de build)
        # tambem e valido
        img_bytes = None
        if self.comp:
            try:
                img_bytes = await asyncio.to_thread(render_comp_card, self.comp)
            except Exception as e:
                await interaction.followup.send(f"❌ Erro gerando a imagem da build: {e}", ephemeral=True)
                return

        try:
            thread = await self.canal.create_thread(
                name=f"\U0001F4CC {titulo}"[:100], type=discord.ChannelType.public_thread, auto_archive_duration=1440,
            )
        except discord.Forbidden:
            await interaction.followup.send("❌ Sem permissão pra criar tópico nesse canal (preciso de 'Criar Tópicos Públicos').", ephemeral=True)
            return

        ping_id = str(uuid.uuid4())
        ping = {
            "ping_id": ping_id,
            "guild_id": interaction.guild.id, "canal_id": self.canal.id, "thread_id": thread.id,
            "mensagem_id": None, "titulo": titulo, "composicao_id": self.comp["id"] if self.comp else None,
            "local": local, "fee": fee, "quando": quando,
            "criado_por": interaction.user.id, "criado_por_nome": interaction.user.display_name,
            "criado_em": time.time(), "funcoes": funcoes,
            "banco": [], "ausentes": [],
            "canal_voz_id": None, "contagem_iniciada": False, "participantes_tempo": {},
        }

        await create_live_event(ping)

        embed = _build_ping_embed(ping)
        view = PingFuncaoView(ping_id, funcoes)
        if img_bytes:
            file = discord.File(io.BytesIO(img_bytes), filename="build.png")
            embed.set_image(url="attachment://build.png")
            msg = await thread.send(content=_ping_copy(titulo, interaction.user), embed=embed, file=file, view=view)
        else:
            msg = await thread.send(content=_ping_copy(titulo, interaction.user), embed=embed, view=view)
        bot.add_view(view)

        # a build de cada funcao nao vai mais publica no topico - ela e enviada
        # no privado de quem clicar em Confirmar (ver handle_ping_button)

        ping["mensagem_id"] = msg.id
        pings = load_pings()
        pings[ping_id] = ping
        save_pings(pings)

        await interaction.followup.send(f"✅ Ping criado em {thread.mention}!", ephemeral=True)


@bot.tree.command(name="ping-conteudo", description="Cria um ping de recrutamento com topico, imagem da build e vagas por reacao")
@app_commands.describe(
    canal="Canal onde o topico vai ser criado (ex: ping-pve, ping-pvp)",
    composicao="Composicao (build) de referencia - opcional, pode deixar vazio e so escrever as vagas",
)
@app_commands.autocomplete(composicao=templates_autocomplete)
@requer_pingar()
async def ping_conteudo(interaction: discord.Interaction, canal: discord.TextChannel, composicao: str = None):
    set_current_guild(interaction.guild_id)
    comp = None
    if composicao:
        templates = load_templates()
        comp = next((t for t in templates if t["id"] == composicao), None)
        if not comp:
            await interaction.response.send_message("❌ Composição não encontrada — escolha uma da lista sugerida ou deixe vazio.", ephemeral=True)
            return
        if not comp.get("slots"):
            await interaction.response.send_message(
                "❌ Essa composição ainda não tem slots/builds. Monte ela em /comp painel ou no site antes, "
                "ou rode o comando de novo sem escolher composição.", ephemeral=True,
            )
            return
    else:
        # sem composicao escolhida - vai direto pro campo de texto livre, sem imagem de build
        await interaction.response.send_modal(PingConteudoModal(canal, None))
        return

    await interaction.response.send_message(
        "Selecione as funções da composição pra preencher as vagas automaticamente, "
        "ou clique em **✏️ Escrever vagas na mão** pra digitar livremente. "
        "Dá pra selecionar e depois ainda editar o campo de texto que abrir, à vontade.",
        view=PingSlotPickerView(canal, comp), ephemeral=True,
    )


async def _refresh_ping_message(channel, ping):
    try:
        message = await channel.fetch_message(ping["mensagem_id"])
    except discord.HTTPException:
        return
    embed = _build_ping_embed(ping)
    try:
        await message.edit(embed=embed)
    except discord.HTTPException:
        pass


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    set_current_guild(payload.guild_id)
    pings = load_pings()
    ping = pings.get(str(payload.message_id))
    if not ping:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    channel = guild.get_channel_or_thread(payload.channel_id)
    if not channel:
        return
    ator = discord.Object(id=payload.user_id)

    if ping.get("encerrado"):
        # ping ja fechado (roster travado) - qualquer reacao nova volta sem efeito
        try:
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(payload.emoji, ator)
        except discord.HTTPException:
            pass
        return

    emoji_str = str(payload.emoji)
    funcao = next((f for f in ping["funcoes"] if f["emoji"] == emoji_str), None)

    if not funcao:
        try:
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(payload.emoji, ator)
        except discord.HTTPException:
            pass
        return

    # so 1 funcao por pessoa - libera qualquer outra que essa pessoa ja tivesse marcado
    funcao_anterior = None
    for outro in ping["funcoes"]:
        if outro is not funcao and payload.user_id in outro["claimed"]:
            outro["claimed"].remove(payload.user_id)
            funcao_anterior = outro
            try:
                message = await channel.fetch_message(payload.message_id)
                await message.remove_reaction(outro["emoji"], ator)
            except discord.HTTPException:
                pass

    if payload.user_id in funcao["claimed"]:
        if funcao_anterior:
            pings[str(payload.message_id)] = ping
            save_pings(pings)
            await _refresh_ping_message(channel, ping)
        return

    if len(funcao["claimed"]) >= funcao["vagas"]:
        try:
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(payload.emoji, ator)
        except discord.HTTPException:
            pass
        try:
            aviso = await channel.send(f"❌ <@{payload.user_id}>, vaga de **{funcao['nome']}** já esgotada.")
            await aviso.delete(delay=8)
        except discord.HTTPException:
            pass
        if funcao_anterior:
            pings[str(payload.message_id)] = ping
            save_pings(pings)
            await _refresh_ping_message(channel, ping)
        return

    funcao["claimed"].append(payload.user_id)
    pings[str(payload.message_id)] = ping
    save_pings(pings)

    try:
        if funcao_anterior:
            # log completo: mostra que a pessoa saiu do cargo anterior E entrou no novo,
            # nao so a confirmacao final - evita duvida de quem ficou onde
            await channel.send(
                f"\U0001F504 <@{payload.user_id}> saiu de **{funcao_anterior['nome']}** "
                f"({len(funcao_anterior['claimed'])}/{funcao_anterior['vagas']}) e entrou em "
                f"**{funcao['nome']}**! {funcao['emoji']} ({len(funcao['claimed'])}/{funcao['vagas']})"
            )
        else:
            await channel.send(f"{funcao['emoji']} <@{payload.user_id}> confirmado como **{funcao['nome']}**! ({len(funcao['claimed'])}/{funcao['vagas']})")
    except discord.HTTPException:
        pass
    await _refresh_ping_message(channel, ping)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    set_current_guild(payload.guild_id)
    pings = load_pings()
    ping = pings.get(str(payload.message_id))
    if not ping or ping.get("encerrado"):
        return
    emoji_str = str(payload.emoji)
    funcao = next((f for f in ping["funcoes"] if f["emoji"] == emoji_str), None)
    if not funcao or payload.user_id not in funcao["claimed"]:
        return
    funcao["claimed"].remove(payload.user_id)
    pings[str(payload.message_id)] = ping
    save_pings(pings)

    guild = bot.get_guild(payload.guild_id)
    channel = guild.get_channel_or_thread(payload.channel_id) if guild else None
    if not channel:
        return
    try:
        # sem auto-delete aqui - faz parte do log de presenca, precisa ficar visivel
        await channel.send(f"\U0001F513 <@{payload.user_id}> liberou a vaga de **{funcao['nome']}**. ({len(funcao['claimed'])}/{funcao['vagas']})")
    except discord.HTTPException:
        pass
    await _refresh_ping_message(channel, ping)


# =========================
# ENCERRAR PING + DIVISAO DE LOOT DO PING
# =========================

async def pings_abertos_autocomplete(interaction: discord.Interaction, current: str):
    set_current_guild(interaction.guild_id)
    pings = load_pings()
    out = [
        app_commands.Choice(name=p["titulo"][:100], value=key)
        for key, p in pings.items()
        if not p.get("encerrado") and current.lower() in p["titulo"].lower()
    ]
    return out[:25]


async def _resolver_nome(guild, uid):
    """Pega o nome de exibicao do membro pra montar a tabela de split. Cai pro fetch
    se nao estiver em cache (bot nao tem intent de members), e por ultimo pro ID puro."""
    if not guild:
        return f"Usuário {uid}"
    membro = guild.get_member(uid)
    if not membro:
        try:
            membro = await guild.fetch_member(uid)
        except discord.HTTPException:
            membro = None
    return membro.display_name if membro else f"Usuário {uid}"


# =========================
# CRIAR EVENTO A PARTIR DE UM PING (ponte ping -> evento)
# =========================

async def _criar_evento_do_ping_core(ping_key, user, guild):
    """Converte um ping de conteudo (recrutamento por botao/reacao) num evento
    de verdade: cria a sala de voz e monta o roster do evento ja com quem
    confirmou presenca no ping, cada um na sua funcao/build ja escolhida.
    A contagem de tempo continua so comecando com /iniciar-evento (ou o botao
    equivalente no painel do evento que acabou de ser criado) - essa conversao
    so monta a sala e carrega o roster, pra ficar justo igual o resto do fluxo.
    Devolve (ok: bool, mensagem: str, event_id: str | None)."""
    pings = load_pings()
    ping = pings.get(ping_key)
    if not ping:
        return False, "❌ Não achei esse ping.", None
    if ping.get("encerrado"):
        return False, "❌ Esse ping já foi encerrado (ou já virou um evento antes).", None
    if not guild:
        return False, "❌ Isso só funciona dentro de um servidor.", None

    pode = (user.id == ping.get("criado_por")) or _pode_pingar(user, guild)
    if not pode:
        return False, "❌ Só quem criou o ping, um caller, um gestor de saldo ou um admin do servidor pode criar o evento a partir dele.", None

    thread = guild.get_channel_or_thread(ping["thread_id"]) if ping.get("thread_id") else None
    canal_destino = thread or guild.get_channel(ping["canal_id"])
    if not canal_destino:
        return False, "❌ Não achei o tópico/canal original desse ping.", None

    category = discord.utils.get(guild.categories, name=EVENTOS_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(EVENTOS_CATEGORY_NAME)
    voice_channel = await guild.create_voice_channel(f"\U0001F53A {ping['titulo']}"[:100], category=category)

    # monta uma "classe" de evento por funcao do ping, reaproveitando a build ja
    # casada pra funcao (mesma logica do DM) pra manter o icone/arma consistente
    classes = []
    for idx, funcao in enumerate(ping["funcoes"]):
        build = _get_build_for_funcao(ping, funcao)
        confirmados_funcao = len(funcao["claimed"]) + len(funcao.get("extra") or [])
        classe = {
            "id": f"pf{idx}_{_slug(funcao['nome']) or idx}",
            "nome": funcao["nome"],
            "cargo": funcao["nome"][:100],
            "item_id": (build or {}).get("item_id"),
            "arma_nome": (build or {}).get("arma_nome") or (build or {}).get("nome"),
            # cobre quem ja estava na 2a prioridade (extra) do ping, senao o
            # evento mostraria a vaga "cheia" mesmo com gente ja dentro dela
            "vagas": max(funcao["vagas"], confirmados_funcao),
            "ordem": idx + 1,
            "role_id": None,
            "emoji_id": None,
            "builds": [build] if build else [],
        }
        try:
            await ensure_class_emoji(classe)
        except Exception:
            pass
        classes.append(classe)

    event_id = f"evt_{voice_channel.id}"
    participantes = {}
    for classe, funcao in zip(classes, ping["funcoes"]):
        role = await ensure_role(guild, classe)
        build = classe["builds"][0] if classe["builds"] else {"nome": classe["nome"]}
        # 1a e 2a prioridade (claimed + extra) entram todo mundo no evento -
        # quem tinha confirmado so de reserva (Banco) ou se marcou ausente fica
        # de fora do roster, por nao ter funcao definida
        for uid in list(funcao["claimed"]) + list(funcao.get("extra") or []):
            nome = await _resolver_nome(guild, uid)
            participantes[str(uid)] = {
                "nome": nome,
                "classe_id": classe["id"],
                "build": build,
                "tempo_total": 0.0,
                "entrou_em": None,
            }
            membro = guild.get_member(uid)
            if membro:
                if role:
                    try:
                        await membro.add_roles(role, reason="Evento criado a partir do ping")
                    except Exception:
                        pass
                if membro.voice and membro.voice.channel and membro.voice.channel.id != voice_channel.id:
                    try:
                        await membro.move_to(voice_channel, reason="Evento criado a partir do ping")
                    except Exception:
                        pass

    events = load_events()
    events[event_id] = {
        "nome": ping["titulo"],
        "guild_id": guild.id,
        "canal_voz_id": voice_channel.id,
        "canal_texto_id": canal_destino.id,
        "mensagem_id": None,
        "status": "aberto",
        "criado_por": user.id,
        "contagem_iniciada": False,
        "criado_em": time.time(),
        "classes": classes,
        "participantes": participantes,
        "origem_ping_id": ping_key,
    }
    save_events(events)

    embed = build_evento_embed(event_id)
    view = EventoView(event_id)
    msg = await canal_destino.send(embed=embed, view=view)
    events = load_events()
    events[event_id]["mensagem_id"] = msg.id
    save_events(events)
    bot.add_view(view)

    ping["encerrado"] = True
    ping["evento_criado_id"] = event_id
    pings = load_pings()
    pings[ping_key] = ping
    save_pings(pings)

    if thread and ping.get("mensagem_id"):
        try:
            painel_msg = await thread.fetch_message(ping["mensagem_id"])
            await painel_msg.edit(view=None)
        except discord.HTTPException:
            pass

    qtd = len(participantes)
    return True, (
        f"\U0001F53A **Evento criado a partir do ping!** Call: {voice_channel.mention} em {canal_destino.mention}. "
        f"{qtd} participante(s) já entraram no roster na própria função. Quando todo mundo estiver na call, use "
        "`/iniciar-evento` (ou o botão no painel do evento) pra começar a contagem de tempo pra todos juntos."
    ), event_id


async def handle_criar_evento_button(interaction: discord.Interaction, ping_id):
    set_current_guild(interaction.guild_id)
    await interaction.response.defer(ephemeral=True, thinking=True)
    ok, msg, _event_id = await _criar_evento_do_ping_core(ping_id, interaction.user, interaction.guild)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="criar-evento", description="[caller] Cria a sala do evento a partir de um ping aberto, com quem ja confirmou")
@app_commands.describe(ping="Qual ping (aberto) usar como base do evento")
@app_commands.autocomplete(ping=pings_abertos_autocomplete)
async def criar_evento_cmd(interaction: discord.Interaction, ping: str):
    set_current_guild(interaction.guild_id)
    await interaction.response.defer(ephemeral=True, thinking=True)
    ok, msg, _event_id = await _criar_evento_do_ping_core(ping, interaction.user, interaction.guild)
    await interaction.followup.send(msg, ephemeral=True)


class PingSplitGateView(discord.ui.View):
    def __init__(self, ping_key):
        super().__init__(timeout=600)
        self.ping_key = ping_key

    @discord.ui.button(label="Sim, vai ter split", style=discord.ButtonStyle.success, emoji="\U0001F4B0")
    async def sim(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(PingProfitModal(self.ping_key))

    @discord.ui.button(label="Não, sem divisão", style=discord.ButtonStyle.secondary)
    async def nao(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.edit_message(content="✅ Ping encerrado. Sem divisão de loot.", view=None)


class PingProfitModal(discord.ui.Modal, title="Valor do profit"):
    profit = discord.ui.TextInput(label="Valor total do profit (prata)", placeholder="Ex: 5000000")

    def __init__(self, ping_key):
        super().__init__()
        self.ping_key = ping_key

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        try:
            valor = float(str(self.profit.value).replace(".", "").replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Valor inválido. Use só números.", ephemeral=True)
            return
        view = PingCompradorView(self.ping_key, valor)
        await interaction.response.send_message(
            f"Profit definido: **{_fmt_prata(valor)} prata**.\n\nAlguém vai comprar o loot (vender o split)?",
            view=view,
        )


class PingCompradorView(discord.ui.View):
    def __init__(self, ping_key, profit):
        super().__init__(timeout=600)
        self.ping_key = ping_key
        self.profit = profit

    @discord.ui.button(label="Sim, alguem vai comprar", style=discord.ButtonStyle.primary)
    async def sim(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(PingReparoModal(self.ping_key, self.profit))

    @discord.ui.button(label="Nao, dividir tudo", style=discord.ButtonStyle.secondary)
    async def nao(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await finalizar_divisao_ping(interaction, self.ping_key, self.profit, has_buyer=False, reparo=0)

    @discord.ui.button(label="💾 Saldar (guardar saldo)", style=discord.ButtonStyle.blurple)
    async def saldar(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(PingSaldarReparoModal(self.ping_key, self.profit))


class PingReparoModal(discord.ui.Modal, title="Valor do reparo"):
    reparo = discord.ui.TextInput(label="Valor do reparo (prata)", placeholder="Ex: 300000")
    taxa_pct = discord.ui.TextInput(label="Taxa de venda do comprador (%)", placeholder="Ex: 15", default="15")

    def __init__(self, ping_key, profit):
        super().__init__()
        self.ping_key = ping_key
        self.profit = profit

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        try:
            valor_reparo = float(str(self.reparo.value).replace(".", "").replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Valor inválido. Use só números.", ephemeral=True)
            return
        try:
            taxa = float(str(self.taxa_pct.value).replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Porcentagem inválida. Use só números (ex: 15).", ephemeral=True)
            return
        await finalizar_divisao_ping(interaction, self.ping_key, self.profit, has_buyer=True, reparo=valor_reparo, taxa_pct=taxa)


class PingSaldarReparoModal(discord.ui.Modal, title="Reparo e taxa (saldar)"):
    """Mesmos campos do PingReparoModal, mas pro fluxo de Saldar: credita o
    saldo de cada um ja descontando reparo/taxa de venda (guild vende o loot
    e paga em prata) - se nao teve custo nenhum, deixa reparo 0 e taxa 0."""
    reparo = discord.ui.TextInput(label="Valor do reparo (prata)", placeholder="Ex: 300000 (ou 0 se nao teve)", default="0")
    taxa_pct = discord.ui.TextInput(label="Taxa de venda (%)", placeholder="Ex: 15 (ou 0 se nao teve)", default="0")

    def __init__(self, ping_key, profit):
        super().__init__()
        self.ping_key = ping_key
        self.profit = profit

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        try:
            valor_reparo = float(str(self.reparo.value).replace(".", "").replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Valor inválido. Use só números.", ephemeral=True)
            return
        try:
            taxa = float(str(self.taxa_pct.value).replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Porcentagem inválida. Use só números (ex: 15, ou 0).", ephemeral=True)
            return
        await saldar_divisao_ping(interaction, self.ping_key, self.profit, reparo=valor_reparo, taxa_pct=taxa)


def _ping_participantes_tempo(ping):
    """Pesos pra divisao do loot do ping. Se a sala propria foi usada
    (_iniciar_sala_ping_core) e tem tempo de call registrado, divide
    proporcional ao tempo - igual o /evento. Senao (ping tradicional, sem
    sala), cai no comportamento antigo: divide igual entre quem confirmou
    uma funcao, 1a e 2a prioridade juntas. Devolve (tempos: dict, por_tempo: bool)."""
    participantes_tempo = ping.get("participantes_tempo") or {}
    tempos_sala = {int(uid): p["tempo_total"] for uid, p in participantes_tempo.items() if p.get("tempo_total", 0) > 0}
    if tempos_sala:
        return tempos_sala, True
    participantes_ids = list(dict.fromkeys(
        uid for f in ping["funcoes"] for uid in (list(f["claimed"]) + list(f.get("extra") or []))
    ))
    return {uid: 1.0 for uid in participantes_ids}, False


async def finalizar_divisao_ping(interaction, ping_key, profit, has_buyer, reparo, taxa_pct=None):
    pings = load_pings()
    ping = pings.get(ping_key)
    if not ping:
        await interaction.response.send_message("Ping não encontrado (talvez já tenha sido removido).", ephemeral=True)
        return

    tempos, por_tempo = _ping_participantes_tempo(ping)
    if not tempos:
        await interaction.response.send_message("Ninguém confirmou presença nesse ping — não dá pra dividir.", ephemeral=True)
        return

    liquido, shares = compute_split(tempos, profit, has_buyer, reparo, taxa_pct)

    guild = interaction.guild
    linhas = []
    for uid, s in sorted(shares.items(), key=lambda kv: kv[1]["valor"], reverse=True):
        nome = await _resolver_nome(guild, uid)
        linhas.append(f"{nome:<20} {s['pct']*100:5.1f}%   {_fmt_prata(s['valor']):>14} prata")

    taxa_exibida = taxa_pct if taxa_pct is not None else BUYER_FEE * 100
    aviso_taxa = f"\nTaxa de {taxa_exibida:.0f}% descontada do valor (reparo: {_fmt_prata(reparo)} prata)." if has_buyer else ""
    modo_txt = "proporcional ao tempo de call" if por_tempo else "igual entre quem confirmou"
    embed = discord.Embed(
        title=f"\U0001F4B0 Divisão do loot · {ping['titulo']}",
        description=(
            f"Profit bruto: {_fmt_prata(profit)} prata{aviso_taxa}\n"
            f"Valor a dividir: **{_fmt_prata(liquido)} prata** ({modo_txt})\n\n"
            "```\n" + "\n".join(linhas) + "\n```"
        ),
        color=0x2ECC71,
    )
    if has_buyer:
        embed.set_footer(text="O comprador paga o valor acima pra cada pessoa (exceto pra si mesmo).")
    embed.timestamp = discord.utils.utcnow()

    ping["loot_dividido"] = True
    ping["loot_profit"] = profit
    ping["loot_liquido"] = liquido
    pings[ping_key] = ping
    save_pings(pings)

    await interaction.response.edit_message(content=None, embed=embed, view=None)


async def saldar_divisao_ping(interaction, ping_key, profit, reparo=0.0, taxa_pct=None):
    """Versao 'saldar' do encerramento de ping: credita o saldo de cada
    participante confirmado em vez de so mostrar o valor a combinar por fora -
    ja descontando reparo/taxa de venda quando a guild vende o loot e paga em
    prata (reparo=0 e taxa_pct=0/None credita o valor cheio, igual antes)."""
    pings = load_pings()
    ping = pings.get(ping_key)
    if not ping:
        await interaction.response.send_message("Ping não encontrado (talvez já tenha sido removido).", ephemeral=True)
        return

    tempos, por_tempo = _ping_participantes_tempo(ping)
    if not tempos:
        await interaction.response.send_message("Ninguém confirmou presença nesse ping — não dá pra dividir.", ephemeral=True)
        return

    tem_desconto = bool(reparo) or bool(taxa_pct)
    liquido, shares = compute_split(tempos, profit, has_buyer=tem_desconto, reparo=reparo, taxa_pct=taxa_pct)

    guild = interaction.guild
    guild_id = interaction.guild_id
    linhas = []
    for uid, s in sorted(shares.items(), key=lambda kv: kv[1]["valor"], reverse=True):
        nome = await _resolver_nome(guild, uid)
        novo_saldo = add_saldo(guild_id, uid, nome, s["valor"], motivo=f"Loot: {ping['titulo']}")
        linhas.append(f"{nome:<20} +{_fmt_prata(s['valor']):>14} prata   (saldo: {_fmt_prata(novo_saldo)})")

    modo_txt = "proporcional ao tempo de call" if por_tempo else "igual entre quem confirmou"
    aviso_desconto = f"\nTaxa de {(taxa_pct or 0):.0f}% + reparo de {_fmt_prata(reparo)} prata descontados antes de creditar." if tem_desconto else ""
    embed = discord.Embed(
        title=f"💾 Loot saldado · {ping['titulo']}",
        description=(
            f"Profit: {_fmt_prata(profit)} prata{aviso_desconto}\n"
            f"Valor creditado: **{_fmt_prata(liquido)} prata** ({modo_txt})\n\n"
            "```\n" + "\n".join(linhas) + "\n```\n"
            "Cada um pode conferir com `/saldo membro`. Ninguem precisa pagar agora."
        ),
        color=0x5865F2,
    )
    embed.timestamp = discord.utils.utcnow()

    ping["loot_dividido"] = True
    ping["loot_profit"] = profit
    ping["loot_liquido"] = liquido
    pings[ping_key] = ping
    save_pings(pings)

    await interaction.response.edit_message(content=None, embed=embed, view=None)


# =========================
# AVALIACAO POS-PING (caller e players avaliam uns aos outros)
# =========================

async def _quem_avaliar(ping, rater_id, guild):
    """Monta a lista de quem `rater_id` ainda pode avaliar nesse ping: o caller
    (se nao for ele mesmo) + cada outro participante confirmado (sem duplicar o
    caller como player tambem). Marca com ✅ quem ja foi avaliado por essa pessoa."""
    caller_id = ping.get("criado_por")
    participantes_ids = list(dict.fromkeys(uid for f in ping["funcoes"] for uid in f["claimed"]))

    ja_avaliados = {
        (a["papel_avaliado"], a["rated_id"])
        for a in load_reputation().get("avaliacoes", [])
        if a["ping_key"] == ping.get("_key") and str(a["rater_id"]) == str(rater_id)
    }

    alvos = []
    if caller_id and caller_id != rater_id:
        nome = await _resolver_nome(guild, caller_id)
        ja = ("caller", str(caller_id)) in ja_avaliados
        alvos.append({"uid": caller_id, "papel": "caller", "nome": nome, "ja": ja})

    for uid in participantes_ids:
        if uid == rater_id or uid == caller_id:
            continue
        nome = await _resolver_nome(guild, uid)
        ja = ("player", str(uid)) in ja_avaliados
        alvos.append({"uid": uid, "papel": "player", "nome": nome, "ja": ja})

    return alvos


async def _build_avaliar_view(ping_key, rater_id, guild):
    """Monta (texto, view) da tela de selecao de quem avaliar, ou None se nao
    sobrou ninguem pra esse avaliador avaliar nesse ping."""
    pings = load_pings()
    ping = pings.get(ping_key)
    if not ping:
        return None
    ping = dict(ping, _key=ping_key)
    alvos = await _quem_avaliar(ping, rater_id, guild)
    if not alvos:
        return None
    texto = "⭐ Quem você quer avaliar? (👍/👎 + comentário opcional)"
    return texto, PingAvaliarSelectView(ping_key, alvos)


class PingAvaliarSelect(discord.ui.Select):
    def __init__(self, ping_key, alvos):
        options = [
            discord.SelectOption(
                label=(f"✅ {a['nome']}" if a["ja"] else a["nome"])[:100],
                value=f"{a['papel']}:{a['uid']}",
                description=("Já avaliado (clique pra corrigir) — " if a["ja"] else "") + ("Caller" if a["papel"] == "caller" else "Player"),
                emoji="\U0001F9ED" if a["papel"] == "caller" else "\U0001F464",
            )
            for a in alvos[:25]
        ]
        super().__init__(placeholder="Escolha quem avaliar...", options=options)
        self.ping_key = ping_key
        self.alvos_by_value = {f"{a['papel']}:{a['uid']}": a for a in alvos}

    async def callback(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        alvo = self.alvos_by_value.get(self.values[0])
        if not alvo:
            await interaction.response.send_message("Opção inválida.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f"Como foi com **{alvo['nome']}** ({'Caller' if alvo['papel'] == 'caller' else 'Player'})?",
            view=PingAvaliarVotoView(self.ping_key, alvo),
        )


class PingAvaliarSelectView(discord.ui.View):
    def __init__(self, ping_key, alvos):
        super().__init__(timeout=300)
        self.add_item(PingAvaliarSelect(ping_key, alvos))


class PingAvaliarVotoView(discord.ui.View):
    def __init__(self, ping_key, alvo):
        super().__init__(timeout=300)
        self.ping_key = ping_key
        self.alvo = alvo

    @discord.ui.button(label="Bom", style=discord.ButtonStyle.success, emoji="\U0001F44D")
    async def positivo(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(PingComentarioModal(self.ping_key, self.alvo, positivo=True))

    @discord.ui.button(label="Ruim", style=discord.ButtonStyle.danger, emoji="\U0001F44E")
    async def negativo(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        await interaction.response.send_modal(PingComentarioModal(self.ping_key, self.alvo, positivo=False))

    @discord.ui.button(label="⬅️ Voltar", style=discord.ButtonStyle.secondary, row=1)
    async def voltar(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_current_guild(interaction.guild_id)
        resultado = await _build_avaliar_view(self.ping_key, interaction.user.id, interaction.guild)
        if not resultado:
            await interaction.response.edit_message(content="✅ Não há mais ninguém pra avaliar por aqui.", view=None)
            return
        texto, view = resultado
        await interaction.response.edit_message(content=texto, view=view)


class PingComentarioModal(discord.ui.Modal, title="Comentário (opcional)"):
    comentario = discord.ui.TextInput(
        label="Comentário", style=discord.TextStyle.paragraph, required=False, max_length=300,
        placeholder="Ex: Chamou muito bem, grupo organizado (deixe em branco se não quiser comentar)",
    )

    def __init__(self, ping_key, alvo, positivo):
        super().__init__()
        self.ping_key = ping_key
        self.alvo = alvo
        self.positivo = positivo

    async def on_submit(self, interaction: discord.Interaction):
        set_current_guild(interaction.guild_id)
        add_rating(
            ping_key=self.ping_key,
            rater_id=interaction.user.id,
            rated_id=self.alvo["uid"],
            papel_avaliado=self.alvo["papel"],
            positivo=self.positivo,
            comentario=str(self.comentario.value),
            rater_name=interaction.user.display_name,
            rated_name=self.alvo["nome"],
        )
        emoji = "\U0001F44D" if self.positivo else "\U0001F44E"
        aviso = f"{emoji} Avaliação de **{self.alvo['nome']}** registrada!\n\n"

        resultado = await _build_avaliar_view(self.ping_key, interaction.user.id, interaction.guild)
        if not resultado:
            await interaction.response.edit_message(content=aviso + "✅ Não há mais ninguém pra avaliar por aqui.", view=None)
            return
        texto, view = resultado
        await interaction.response.edit_message(content=aviso + texto, view=view)


class PingAvaliarView(discord.ui.View):
    """Botao persistente (fica no roster final do ping) que abre a avaliacao
    ephemera pra quem clicar. custom_id carrega o ping_key pra sobreviver a
    restart do bot (re-registrado em on_ready pros pings ja encerrados)."""
    def __init__(self, ping_key):
        super().__init__(timeout=None)
        btn = discord.ui.Button(
            label="Avaliar participantes", style=discord.ButtonStyle.primary, emoji="⭐",
            custom_id=f"pingavaliar:{ping_key}",
        )
        btn.callback = self._make_callback(ping_key)
        self.add_item(btn)

    def _make_callback(self, ping_key):
        async def callback(interaction: discord.Interaction):
            set_current_guild(interaction.guild_id)
            resultado = await _build_avaliar_view(ping_key, interaction.user.id, interaction.guild)
            if not resultado:
                await interaction.response.send_message("Não há ninguém pra você avaliar nesse ping (ou você já avaliou todo mundo).", ephemeral=True)
                return
            texto, view = resultado
            await interaction.response.send_message(texto, view=view, ephemeral=True)
        return callback


@bot.tree.command(name="perfil", description="Mostra a reputacao de um jogador (avaliacoes como caller e como player)")
@app_commands.describe(membro="De quem ver o perfil (deixe vazio pra ver o seu)")
async def perfil(interaction: discord.Interaction, membro: discord.Member = None):
    set_current_guild(interaction.guild_id)
    alvo = membro or interaction.user
    stats = compute_reputation_stats(alvo.id)

    if stats["total"] == 0:
        await interaction.response.send_message(f"**{alvo.display_name}** ainda não tem nenhuma avaliação.", ephemeral=True)
        return

    embed = discord.Embed(title=f"⭐ Perfil de reputação · {alvo.display_name}", color=GOLD)
    embed.set_thumbnail(url=alvo.display_avatar.url)

    embed.add_field(
        name="Geral",
        value=f"{stats['positivos']}\U0001F44D / {stats['negativos']}\U0001F44E  ·  **{stats['pct_positivo']}%** positivo  ·  {stats['total']} avaliações",
        inline=False,
    )

    caller = stats["como_caller"]
    if caller["total"]:
        embed.add_field(
            name="\U0001F9ED Como Caller",
            value=f"{caller['positivos']}\U0001F44D / {caller['negativos']}\U0001F44E  ({caller['pct_positivo']}%, {caller['total']})",
            inline=True,
        )
    player = stats["como_player"]
    if player["total"]:
        embed.add_field(
            name="\U0001F464 Como Player",
            value=f"{player['positivos']}\U0001F44D / {player['negativos']}\U0001F44E  ({player['pct_positivo']}%, {player['total']})",
            inline=True,
        )

    if stats["badges"]:
        embed.add_field(name="Badges", value=" ".join(stats["badges"]), inline=False)

    if stats["ultimos_positivos"]:
        texto = "\n".join(f"👍 “{a['comentario']}”" for a in stats["ultimos_positivos"])
        embed.add_field(name="Comentários recentes", value=texto[:1024], inline=False)

    if stats["ultimos_negativos"]:
        texto = "\n".join(f"👎 “{a['comentario']}”" for a in stats["ultimos_negativos"])
        embed.add_field(name="Pontos de atenção", value=texto[:1024], inline=False)

    embed.set_footer(text="Avaliações vêm dos pings de conteúdo encerrados com /encerrar-ping")
    await interaction.response.send_message(embed=embed)


@saldo_group.command(name="membro", description="Mostra seu saldo acumulado de loot saldado")
async def saldo_membro(interaction: discord.Interaction):
    set_current_guild(interaction.guild_id)
    data = load_saldos(interaction.guild_id)
    saldos = data.get("saldos", {})
    entrada = saldos.get(str(interaction.user.id))
    valor = entrada.get("valor", 0.0) if entrada else 0.0

    embed = discord.Embed(
        title="\U0001F4BE Seu saldo",
        description=(
            f"Saldo acumulado: **{_fmt_prata(valor)} prata**\n"
            "Esse valor vem dos pings/eventos onde o loot foi saldado (em vez de pago na hora)."
        ),
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@saldo_group.command(name="todos", description="[gestor de saldo] Mostra o saldo total e o saldo de cada membro da guilda")
async def saldo_todos(interaction: discord.Interaction):
    set_current_guild(interaction.guild_id)
    if not _pode_saldo(interaction.user, interaction.guild):
        await interaction.response.send_message(
            MSG_SEM_SALDO,
            ephemeral=True,
        )
        return

    data = load_saldos(interaction.guild_id)
    saldos = data.get("saldos", {})
    com_saldo = {uid: e for uid, e in saldos.items() if e.get("valor", 0.0) != 0}

    embed = discord.Embed(title="\U0001F4CA Saldo geral da guilda", color=0x5865F2)
    if com_saldo:
        total_bruto = sum(e.get("valor", 0.0) for e in com_saldo.values())
        linhas = [
            f"<@{uid}> — **{_fmt_prata(e['valor'])} prata**"
            for uid, e in sorted(com_saldo.items(), key=lambda kv: kv[1]["valor"], reverse=True)
        ]
        geral_txt = f"Total bruto: **{_fmt_prata(total_bruto)} prata** ({len(com_saldo)} pessoa(s))\n\n" + "\n".join(linhas)
        if len(geral_txt) > 4000:
            geral_txt = geral_txt[:4000] + "\n…(lista cortada, use `/saldo-consultar` pra ver alguem especifico)"
        embed.description = geral_txt
    else:
        embed.description = "Ninguém tem saldo acumulado no momento."

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="saldo-consultar", description="[gestor de saldo] Mostra o saldo acumulado de um membro")
@app_commands.describe(membro="De quem ver o saldo")
async def saldo_consultar(interaction: discord.Interaction, membro: discord.Member):
    set_current_guild(interaction.guild_id)
    if not _pode_saldo(interaction.user, interaction.guild):
        await interaction.response.send_message(
            MSG_SEM_SALDO,
            ephemeral=True,
        )
        return
    data = load_saldos(interaction.guild_id)
    entrada = data.get("saldos", {}).get(str(membro.id))
    valor = entrada.get("valor", 0.0) if entrada else 0.0
    await interaction.response.send_message(
        f"\U0001F4BE Saldo de **{membro.display_name}**: **{_fmt_prata(valor)} prata**", ephemeral=True,
    )


@bot.tree.command(name="saldo-quitar", description="[gestor de saldo] Da baixa no saldo de um membro (marca como pago fora do bot)")
@app_commands.describe(membro="Quem foi pago", valor="Valor pago em prata (deixe vazio pra zerar o saldo inteiro)")
async def saldo_quitar(interaction: discord.Interaction, membro: discord.Member, valor: str = None):
    set_current_guild(interaction.guild_id)
    if not _pode_saldo(interaction.user, interaction.guild):
        await interaction.response.send_message(
            MSG_SEM_SALDO,
            ephemeral=True,
        )
        return
    valor_num = None
    if valor:
        try:
            valor_num = float(str(valor).replace(".", "").replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Valor inválido. Use só números, ou deixe vazio pra zerar tudo.", ephemeral=True)
            return
    novo_saldo = quitar_saldo(
        interaction.guild_id, membro.id, interaction.user.id, por_nome=interaction.user.display_name, valor=valor_num,
    )
    await interaction.response.send_message(
        f"✅ Baixa registrada pra **{membro.display_name}**. Novo saldo: **{_fmt_prata(novo_saldo)} prata**.", ephemeral=True,
    )


@bot.tree.command(name="saldo-gestor", description="[admin] Adiciona ou remove alguém como gestor de saldo dessa guilda")
@app_commands.describe(acao="adicionar ou remover", membro="Quem vai virar/deixar de ser gestor de saldo")
@app_commands.choices(acao=[
    app_commands.Choice(name="adicionar", value="adicionar"),
    app_commands.Choice(name="remover", value="remover"),
])
async def saldo_gestor(interaction: discord.Interaction, acao: app_commands.Choice[str], membro: discord.Member):
    set_current_guild(interaction.guild_id)
    if not _eh_admin(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "❌ Só admin do servidor decide quem é gestor de saldo. Também dá pra fazer isso pelo site, no painel do servidor.",
            ephemeral=True,
        )
        return
    gestores = [str(g) for g in load_saldo_gestores(interaction.guild_id)]
    alvo = str(membro.id)
    if acao.value == "adicionar":
        if alvo not in gestores:
            gestores.append(alvo)
            save_saldo_gestores(gestores, interaction.guild_id)
        await interaction.response.send_message(f"✅ **{membro.display_name}** agora é gestor de saldo.", ephemeral=True)
    else:
        if alvo in gestores:
            gestores.remove(alvo)
            save_saldo_gestores(gestores, interaction.guild_id)
        await interaction.response.send_message(f"✅ **{membro.display_name}** não é mais gestor de saldo.", ephemeral=True)



@saldo_group.command(name="ajustar", description="[gestor de saldo] Adiciona ou remove prata do saldo de um membro")
@app_commands.describe(
    membro="De quem é o saldo",
    valor="Quanto. Positivo adiciona, negativo remove (ex: 500000 ou -200000)",
    motivo="Por que (aparece no histórico)",
)
async def saldo_ajustar(interaction: discord.Interaction, membro: discord.Member, valor: str, motivo: str = None):
    set_current_guild(interaction.guild_id)
    if not _pode_saldo(interaction.user, interaction.guild):
        await interaction.response.send_message(MSG_SEM_SALDO, ephemeral=True)
        return
    try:
        bruto = str(valor).strip().replace(" ", "")
        sinal = -1 if bruto.startswith("-") else 1
        num = float(bruto.lstrip("+-").replace(".", "").replace(",", ".")) * sinal
    except ValueError:
        await interaction.response.send_message("Valor inválido. Use só números, com `-` na frente pra remover.", ephemeral=True)
        return
    if num == 0:
        await interaction.response.send_message("O valor precisa ser diferente de zero.", ephemeral=True)
        return
    novo = ajustar_saldo(
        interaction.guild_id, membro.id, membro.display_name, num, motivo or "Ajuste manual",
        interaction.user.id, por_nome=interaction.user.display_name,
    )
    acao = "Adicionado" if num > 0 else "Removido"
    await interaction.response.send_message(
        f"✅ {acao} **{_fmt_prata(abs(num))} prata** de **{membro.display_name}**. Saldo agora: **{_fmt_prata(novo)} prata**.",
        ephemeral=True,
    )


NIVEL_CHOICES = [
    app_commands.Choice(name="caller (pode pingar)", value="caller"),
    app_commands.Choice(name="gestor de saldo (saldo e pingar)", value="gestor"),
]
NOME_NIVEL = {"caller": "caller", "gestor": "gestor de saldo"}


async def _so_admin(interaction):
    set_current_guild(interaction.guild_id)
    if _eh_admin(interaction.user, interaction.guild):
        return True
    await interaction.response.send_message(
        "❌ Só admin do servidor (Administrador ou Gerenciar Servidor) mexe nas permissões da equipe.", ephemeral=True,
    )
    return False


@permissao_group.command(name="adicionar", description="[admin] Libera uma pessoa como caller ou gestor de saldo")
@app_commands.describe(nivel="O que ela vai poder fazer", membro="Quem")
@app_commands.choices(nivel=NIVEL_CHOICES)
async def permissao_adicionar(interaction: discord.Interaction, nivel: app_commands.Choice[str], membro: discord.Member):
    if not await _so_admin(interaction):
        return
    set_permissao(interaction.guild_id, nivel.value, "pessoas", membro.id, adicionar=True)
    await interaction.response.send_message(f"✅ **{membro.display_name}** agora é **{NOME_NIVEL[nivel.value]}**.", ephemeral=True)


@permissao_group.command(name="remover", description="[admin] Tira uma pessoa de caller ou de gestor de saldo")
@app_commands.describe(nivel="De qual nível tirar", membro="Quem")
@app_commands.choices(nivel=NIVEL_CHOICES)
async def permissao_remover(interaction: discord.Interaction, nivel: app_commands.Choice[str], membro: discord.Member):
    if not await _so_admin(interaction):
        return
    mudou = set_permissao(interaction.guild_id, nivel.value, "pessoas", membro.id, adicionar=False)
    txt = f"✅ **{membro.display_name}** não é mais **{NOME_NIVEL[nivel.value]}**." if mudou else f"**{membro.display_name}** já não era {NOME_NIVEL[nivel.value]}."
    await interaction.response.send_message(txt, ephemeral=True)


@permissao_group.command(name="cargo", description="[admin] Faz todo mundo de um cargo virar caller ou gestor de saldo (ou desfaz)")
@app_commands.describe(nivel="O que o cargo libera", cargo="Qual cargo do Discord", acao="Ligar ou desligar")
@app_commands.choices(nivel=NIVEL_CHOICES, acao=[
    app_commands.Choice(name="ligar", value="ligar"),
    app_commands.Choice(name="desligar", value="desligar"),
])
async def permissao_cargo(interaction: discord.Interaction, nivel: app_commands.Choice[str], cargo: discord.Role, acao: app_commands.Choice[str]):
    if not await _so_admin(interaction):
        return
    ligar = acao.value == "ligar"
    set_permissao(interaction.guild_id, nivel.value, "cargos", cargo.id, adicionar=ligar)
    if ligar:
        txt = f"✅ Quem tem o cargo {cargo.mention} agora é **{NOME_NIVEL[nivel.value]}**."
    else:
        txt = f"✅ O cargo {cargo.mention} não libera mais **{NOME_NIVEL[nivel.value]}**."
    await interaction.response.send_message(txt, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@permissao_group.command(name="criar-cargos", description="[admin] Cria os cargos Caller e Gestor de Saldo já ligados às permissões")
async def permissao_criar_cargos(interaction: discord.Interaction):
    if not await _so_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    linhas = []
    for nivel, nome, cor in (("caller", "Caller", 0xE2BC62), ("gestor", "Gestor de Saldo", 0x6FCF8E)):
        cargo = discord.utils.get(interaction.guild.roles, name=nome)
        if cargo is None:
            try:
                cargo = await interaction.guild.create_role(name=nome, colour=discord.Colour(cor), mentionable=True,
                                                            reason=f"/permissao criar-cargos por {interaction.user}")
                linhas.append(f"Criei o cargo {cargo.mention}.")
            except discord.Forbidden:
                linhas.append(f"❌ Não consegui criar **{nome}**: o bot precisa da permissão Gerenciar Cargos.")
                continue
        else:
            linhas.append(f"O cargo {cargo.mention} já existia, usei ele.")
        set_permissao(interaction.guild_id, nivel, "cargos", cargo.id, adicionar=True)
    linhas.append("\nAgora é só dar esses cargos pra equipe. Caller pinga; Gestor de Saldo pinga e mexe no saldo; "
                  "admin do servidor faz tudo.")
    await interaction.followup.send("\n".join(linhas), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@permissao_group.command(name="listar", description="Mostra quem é caller e quem é gestor de saldo")
async def permissao_listar(interaction: discord.Interaction):
    set_current_guild(interaction.guild_id)
    perms = load_permissoes(interaction.guild_id)
    embed = discord.Embed(title="Equipe do bot", color=0xE2BC62,
                          description="**Admin do servidor** (Administrador ou Gerenciar Servidor) faz tudo.")
    for nivel, titulo, oque in (("gestor", "Gestores de saldo", "mexem no saldo e pingam"), ("caller", "Callers", "pingam, criam eventos e montam composições")):
        cargos = [f"<@&{c}>" for c in perms[nivel]["cargos"]]
        pessoas = [f"<@{p}>" for p in perms[nivel]["pessoas"]]
        valor = (f"Cargos: {', '.join(cargos)}\n" if cargos else "") + (f"Pessoas: {', '.join(pessoas)}" if pessoas else "")
        embed.add_field(name=f"{titulo} ({oque})", value=(valor or "Ninguém ainda.")[:1024], inline=False)
    meu = _nivel(interaction.user, interaction.guild)
    embed.set_footer(text=f"Seu nível: {({'admin': 'admin', 'gestor': 'gestor de saldo', 'caller': 'caller'}).get(meu, 'membro')}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, SemPermissao):
        msg = str(error)
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "❌ Você não tem permissão pra usar esse comando."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "❌ Você não tem permissão pra usar esse comando."
    else:
        print(f"[erro] comando /{interaction.command.qualified_name if interaction.command else '?'}: {error!r}")
        msg = "❌ Deu um erro ao rodar esse comando. Tenta de novo em instantes."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass

async def _encerrar_ping_core(interaction: discord.Interaction, ping_key):
    """Trava as vagas de um ping, posta o roster final e pergunta se vai ter
    divisao de loot. Usado tanto pelo /encerrar-ping quanto pelo botao Config
    do painel do ping."""
    pings = load_pings()
    alvo = pings.get(ping_key) if ping_key else None
    if not alvo:
        await interaction.response.send_message(
            "❌ Não achei esse ping. Rode o comando dentro do tópico do ping ou escolha um da lista.", ephemeral=True,
        )
        return
    if alvo.get("encerrado"):
        await interaction.response.send_message("Esse ping já foi encerrado.", ephemeral=True)
        return

    alvo["encerrado"] = True

    # se esse ping ganhou sala propria (_iniciar_sala_ping_core), fecha a
    # contagem de tempo igual o /evento finalizar faz: soma o que falta de
    # quem ainda ta na call e apaga a sala - o tempo registrado aqui e o que
    # vira peso no split proporcional (ver finalizar_divisao_ping)
    tempo_resumo_txt = ""
    guild = bot.get_guild(alvo["guild_id"])
    if alvo.get("canal_voz_id"):
        agora = time.time()
        participantes_tempo = alvo.get("participantes_tempo") or {}
        for p in participantes_tempo.values():
            if p.get("entrou_em"):
                p["tempo_total"] += agora - p["entrou_em"]
                p["entrou_em"] = None
        voice_channel = guild.get_channel(alvo["canal_voz_id"]) if guild else None
        if voice_channel:
            try:
                await voice_channel.delete(reason="Ping encerrado")
            except Exception:
                pass
        total_tempo = sum(p.get("tempo_total", 0) for p in participantes_tempo.values())
        if total_tempo > 0 and guild:
            linhas_tempo = []
            for uid, p in sorted(participantes_tempo.items(), key=lambda kv: kv[1].get("tempo_total", 0), reverse=True):
                if p.get("tempo_total", 0) <= 0:
                    continue
                nome = await _resolver_nome(guild, int(uid))
                pct = p["tempo_total"] / total_tempo
                linhas_tempo.append(f"{nome:<20} {_fmt_duration(p['tempo_total']):>8}  {pct*100:5.1f}%")
            tempo_resumo_txt = (
                "\n\n\U0001F50A **Tempo de call** (vai definir o split proporcional):\n```\n"
                + "\n".join(linhas_tempo) + "\n```"
            )

    pings[ping_key] = alvo
    save_pings(pings)

    await complete_live_event(alvo)

    thread = guild.get_channel_or_thread(alvo["thread_id"]) if guild else None
    if thread:
        # trava os botoes do painel original (ninguem mais consegue clicar)
        if alvo.get("mensagem_id"):
            try:
                painel_msg = await thread.fetch_message(alvo["mensagem_id"])
                await painel_msg.edit(view=None)
            except discord.HTTPException:
                pass

        total_confirmados = sum(len(f["claimed"]) for f in alvo["funcoes"])
        resumo = "\n".join(
            f"{f['emoji']} **{f['nome']}**: " + (" ".join(f"<@{uid}>" for uid in f["claimed"]) or "ninguém")
            for f in alvo["funcoes"]
        )
        embed = discord.Embed(
            title=f"\U0001F512 Ping encerrado · {alvo['titulo']}",
            description=f"Roster final ({total_confirmados} confirmados). As vagas estão travadas:\n\n{resumo}{tempo_resumo_txt}",
            color=0xE74C3C,
        )
        embed.set_footer(text="Clique em ⭐ pra avaliar o caller e quem jogou com você")
        try:
            await thread.send(embed=embed, view=PingAvaliarView(ping_key))
        except discord.HTTPException:
            pass

    await interaction.response.send_message(
        "Ping encerrado. Vai ter divisão de loot desse conteúdo?",
        view=PingSplitGateView(ping_key),
    )


@bot.tree.command(name="encerrar-ping", description="Encerra um ping de conteudo (trava as vagas) e, se quiser, abre a divisao de loot")
@app_commands.describe(ping="Qual ping encerrar (deixe vazio se estiver rodando dentro do tópico do ping)")
@app_commands.autocomplete(ping=pings_abertos_autocomplete)
@requer_pingar()
async def encerrar_ping(interaction: discord.Interaction, ping: str = None):
    set_current_guild(interaction.guild_id)
    pings = load_pings()
    ping_key = ping or next(
        (k for k, p in pings.items() if p.get("thread_id") == interaction.channel_id and not p.get("encerrado")),
        None,
    )
    await _encerrar_ping_core(interaction, ping_key)


@evento_group.command(name="iniciar", description="Inicia um novo evento agora: cria a call e o painel de classes/composicao")
@app_commands.describe(nome="Nome do evento (ex: ZvZ Terca, Avalon Roads)", template="Composicao a usar (vazio = roster padrao de /classes)")
@app_commands.autocomplete(template=templates_autocomplete)
@requer_pingar()
async def evento_iniciar(interaction: discord.Interaction, nome: str, template: str = None):
    set_current_guild(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    slots = await _build_event_slots(template)
    if slots is None:
        await interaction.followup.send("Composicao nao encontrada.", ephemeral=True)
        return

    category = discord.utils.get(guild.categories, name=EVENTOS_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(EVENTOS_CATEGORY_NAME)

    voice_channel = await guild.create_voice_channel(f"\U0001F53A {nome}", category=category)

    event_id = f"evt_{voice_channel.id}"
    events = load_events()
    events[event_id] = {
        "nome": nome,
        "guild_id": guild.id,
        "canal_voz_id": voice_channel.id,
        "canal_texto_id": interaction.channel.id,
        "mensagem_id": None,
        "status": "aberto",
        "criado_por": interaction.user.id,
        "contagem_iniciada": False,
        "criado_em": time.time(),
        "classes": slots,
        "participantes": {},
    }
    save_events(events)

    embed = build_evento_embed(event_id)
    view = EventoView(event_id)
    msg = await interaction.channel.send(embed=embed, view=view)
    events[event_id]["mensagem_id"] = msg.id
    save_events(events)
    bot.add_view(view)

    await interaction.followup.send(
        f"✅ Evento **{nome}** criado! Call: {voice_channel.mention}\nQuando todo mundo entrar na call, rode `/iniciar-evento` pra começar a contagem de tempo pra todos ao mesmo tempo.",
        ephemeral=True,
    )


@evento_group.command(name="agendar", description="Agenda um evento futuro: abre inscricao agora, a call e criada sozinha na hora")
@app_commands.describe(
    nome="Nome do evento",
    data="Data (DD/MM ou DD/MM/AAAA)",
    hora="Hora (HH:MM, horario de Brasilia)",
    template="Composicao a usar (vazio = roster padrao de /classes)",
)
@app_commands.autocomplete(template=templates_autocomplete)
@requer_pingar()
async def evento_agendar(interaction: discord.Interaction, nome: str, data: str, hora: str, template: str = None):
    set_current_guild(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)
    try:
        dt_local = _parse_data_hora(data, hora)
    except Exception:
        await interaction.followup.send("Data/hora invalida. Use DD/MM ou DD/MM/AAAA pra data e HH:MM pra hora.", ephemeral=True)
        return
    if dt_local.timestamp() <= time.time():
        await interaction.followup.send("Essa data/hora ja passou.", ephemeral=True)
        return

    slots = await _build_event_slots(template)
    if slots is None:
        await interaction.followup.send("Composicao nao encontrada.", ephemeral=True)
        return

    event_id = f"evt_{interaction.id}"
    events = load_events()
    events[event_id] = {
        "nome": nome,
        "guild_id": interaction.guild.id,
        "status": "agendado",
        "quando": dt_local.timestamp(),
        "alertado_5min": False,
        "canal_voz_id": None,
        "canal_texto_id": interaction.channel.id,
        "mensagem_id": None,
        "criado_por": interaction.user.id,
        "contagem_iniciada": False,
        "criado_em": time.time(),
        "classes": slots,
        "participantes": {},
    }
    save_events(events)

    embed = build_evento_embed(event_id)
    view = EventoView(event_id)
    msg = await interaction.channel.send(embed=embed, view=view)
    events = load_events()
    events[event_id]["mensagem_id"] = msg.id
    save_events(events)
    bot.add_view(view)

    await interaction.followup.send(f"\U0001F4C5 Evento **{nome}** agendado pra {dt_local.strftime('%d/%m %H:%M')}. Inscricoes abertas!", ephemeral=True)


@evento_group.command(name="agenda", description="Mostra os proximos eventos agendados")
async def evento_agenda(interaction: discord.Interaction):
    set_current_guild(interaction.guild_id)
    events = load_events()
    agendados = [(eid, e) for eid, e in events.items() if e.get("status") == "agendado"]
    agendados.sort(key=lambda kv: kv[1]["quando"])
    if not agendados:
        await interaction.response.send_message("Nenhum evento agendado no momento.", ephemeral=True)
        return
    linhas = []
    for eid, e in agendados:
        dt = datetime.fromtimestamp(e["quando"], TIMEZONE)
        linhas.append(f"\U0001F4C5 **{e['nome']}** — {dt.strftime('%d/%m às %H:%M')} — {len(e['participantes'])} inscrito(s)")
    embed = discord.Embed(title="\U0001F5D3️ Proximos eventos", description="\n".join(linhas), color=GOLD)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@evento_group.command(name="finalizar", description="Encerra o evento, calcula o tempo de cada um e inicia a divisao do loot")
@app_commands.describe(evento="Qual evento encerrar")
@app_commands.autocomplete(evento=eventos_abertos_autocomplete)
@requer_pingar()
async def evento_finalizar(interaction: discord.Interaction, evento: str):
    set_current_guild(interaction.guild_id)
    events = load_events()
    event = events.get(evento)
    if not event or event["status"] != "aberto":
        await interaction.response.send_message("Evento nao encontrado ou ja encerrado.", ephemeral=True)
        return

    await interaction.response.defer()

    guild = interaction.guild
    now = time.time()
    for uid, p in event["participantes"].items():
        if p.get("entrou_em"):
            p["tempo_total"] += now - p["entrou_em"]
            p["entrou_em"] = None
        member = guild.get_member(int(uid))
        if member:
            for c in event["classes"]:
                if c.get("role_id"):
                    role = guild.get_role(int(c["role_id"]))
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Evento encerrado")
                        except Exception:
                            pass

    event["status"] = "encerrado"
    save_events(events)

    voice_channel = guild.get_channel(event["canal_voz_id"])
    if voice_channel:
        try:
            await voice_channel.delete(reason="Evento encerrado")
        except Exception:
            pass

    total_time = sum(p["tempo_total"] for p in event["participantes"].values())
    if total_time <= 0:
        await interaction.followup.send("Evento encerrado, mas ninguem teve tempo registrado — sem divisao de loot.")
        _archive_event(evento, events)
        return

    linhas = []
    for uid, p in sorted(event["participantes"].items(), key=lambda kv: kv[1]["tempo_total"], reverse=True):
        pct = p["tempo_total"] / total_time
        linhas.append(f"{p['nome']:<20} {_fmt_duration(p['tempo_total']):>8}  {pct*100:5.1f}%")

    resumo = "```\n" + "\n".join(linhas) + "\n```"
    embed = discord.Embed(
        title=f"\U0001F3C1 Evento encerrado: {event['nome']}",
        description=f"Tempo total: {_fmt_duration(total_time)}\n\n{resumo}",
        color=GOLD,
    )
    embed.set_footer(text="Clique abaixo pra lancar o valor do profit e dividir o loot.")

    view = FinalizarLootView(evento)
    await interaction.followup.send(embed=embed, view=view)


@evento_group.command(name="cancelar", description="Cancela um evento sem dividir loot (ex: criado por engano)")
@app_commands.autocomplete(evento=eventos_abertos_autocomplete)
@requer_pingar()
async def evento_cancelar(interaction: discord.Interaction, evento: str):
    set_current_guild(interaction.guild_id)
    events = load_events()
    event = events.get(evento)
    if not event:
        await interaction.response.send_message("Evento nao encontrado.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    for uid, p in event["participantes"].items():
        member = guild.get_member(int(uid))
        if member:
            for c in event["classes"]:
                if c.get("role_id"):
                    role = guild.get_role(int(c["role_id"]))
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Evento cancelado")
                        except Exception:
                            pass
    voice_channel = guild.get_channel(event["canal_voz_id"])
    if voice_channel:
        try:
            await voice_channel.delete(reason="Evento cancelado")
        except Exception:
            pass
    _archive_event(evento, events)
    await interaction.followup.send(f"\U0001F5D1️ Evento **{event['nome']}** cancelado.", ephemeral=True)


@evento_group.command(name="status", description="Mostra o andamento (tempo/participacao) de um evento aberto")
@app_commands.autocomplete(evento=eventos_abertos_autocomplete)
async def evento_status(interaction: discord.Interaction, evento: str):
    set_current_guild(interaction.guild_id)
    events = load_events()
    event = events.get(evento)
    if not event:
        await interaction.response.send_message("Evento nao encontrado.", ephemeral=True)
        return
    now = time.time()
    linhas = []
    for p in event["participantes"].values():
        tempo = p["tempo_total"] + ((now - p["entrou_em"]) if p.get("entrou_em") else 0)
        status = "\U0001F7E2" if p.get("entrou_em") else "⚪"
        build_nome = (p.get("build") or {}).get("nome") or p["classe_id"]
        linhas.append(f"{status} {p['nome']:<20} {_fmt_duration(tempo):>8}  ({build_nome})")
    if not linhas:
        linhas = ["Nenhum participante ainda."]
    aviso_contagem = "" if event.get("contagem_iniciada") else "\n⚠️ Contagem ainda não iniciada — use `/iniciar-evento` quando todos estiverem na call."
    await interaction.response.send_message("```\n" + "\n".join(linhas) + "\n```" + aviso_contagem, ephemeral=True)


async def _iniciar_evento_core(event_id, user, guild):
    """Logica compartilhada entre o comando /iniciar-evento e o botao "Iniciar
    evento" no painel do evento: valida permissao/estado e sincroniza o inicio
    da contagem pra quem ja esta na call. Devolve (ok: bool, mensagem: str)."""
    events = load_events()
    event = events.get(event_id)
    if not event or event.get("status") != "aberto":
        return False, "❌ Evento não encontrado ou a call ainda não foi criada."

    pode_iniciar = (user.id == event.get("criado_por")) or _pode_pingar(user, guild)
    if not pode_iniciar:
        return False, "❌ Só quem criou o evento, um caller, um gestor de saldo ou um admin do servidor pode iniciar a contagem."

    if event.get("contagem_iniciada"):
        return False, "⚠️ A contagem desse evento já foi iniciada."

    voice_channel = guild.get_channel(event["canal_voz_id"]) if event.get("canal_voz_id") else None
    if not voice_channel:
        return False, "❌ Não achei a call desse evento."

    agora = time.time()
    ids_na_call = {str(m.id) for m in voice_channel.members}
    qtd = 0
    for uid, p in event["participantes"].items():
        if uid in ids_na_call and not p.get("entrou_em"):
            p["entrou_em"] = agora
            qtd += 1
    event["contagem_iniciada"] = True
    save_events(events)
    await refresh_evento_panel(event_id)

    return True, (
        f"▶️ **Contagem iniciada!** {qtd} participante(s) já na call **{voice_channel.name}** "
        "começam a contar agora, todo mundo ao mesmo tempo. Quem entrar depois conta a partir do próprio ingresso."
    )


async def handle_iniciar_evento_button(interaction: discord.Interaction, event_id):
    ok, msg = await _iniciar_evento_core(event_id, interaction.user, interaction.guild)
    await interaction.response.send_message(msg, ephemeral=not ok)


@bot.tree.command(name="iniciar-evento", description="[caller] Inicia a contagem de tempo do evento pra quem ja esta na call, todos juntos")
@app_commands.describe(evento="Qual evento comecar a contagem")
@app_commands.autocomplete(evento=eventos_abertos_autocomplete)
async def iniciar_evento(interaction: discord.Interaction, evento: str):
    set_current_guild(interaction.guild_id)
    ok, msg = await _iniciar_evento_core(evento, interaction.user, interaction.guild)
    await interaction.response.send_message(msg, ephemeral=not ok)


@bot.tree.command(name="sync", description="[admin] Forca a sincronizacao instantanea dos comandos neste servidor")
@app_commands.checks.has_permissions(administrator=True)
async def sync_cmd(interaction: discord.Interaction):
    set_current_guild(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)
    bot.tree.copy_global_to(guild=interaction.guild)
    synced = await bot.tree.sync(guild=interaction.guild)
    await interaction.followup.send(f"✅ {len(synced)} comandos sincronizados neste servidor.", ephemeral=True)


# =========================
# /comandos + checagem de permissoes
# =========================

FRONTEND_URL_PUBLICO = os.environ.get("FRONTEND_URL", "https://seagreen-rat-589164.hostingersite.com")

# Permissoes que o bot usa de verdade (criar categoria/call/role, mover membro
# entre calls, abrir topico, mandar embed/imagem, marcar cargo).
PERMISSOES_NECESSARIAS = {
    "view_channel": "Ver canais",
    "send_messages": "Enviar mensagens",
    "send_messages_in_threads": "Enviar mensagens em tópicos",
    "create_public_threads": "Criar tópicos públicos",
    "manage_threads": "Gerenciar tópicos",
    "embed_links": "Inserir links",
    "attach_files": "Anexar arquivos",
    "read_message_history": "Ver histórico de mensagens",
    "mention_everyone": "Mencionar @everyone e todos os cargos",
    "add_reactions": "Adicionar reações",
    "use_external_emojis": "Usar emojis externos",
    "manage_channels": "Gerenciar canais",
    "manage_roles": "Gerenciar cargos",
    "connect": "Conectar (voz)",
    "move_members": "Mover membros",
}

# Ordem e titulo de cada secao do /comandos (pelo nome do comando raiz).
SECOES_COMANDOS = [
    ("Eventos", ["evento", "criar-evento", "iniciar-evento"]),
    ("Pings de conteúdo", ["ping-conteudo", "encerrar-ping", "ping-funcao"]),
    ("Composições", ["comp"]),
    ("Classes (roster simples)", ["classes"]),
    ("Saldo e loot", ["saldo", "saldo-consultar", "saldo-quitar"]),
    ("Equipe e permissões", ["permissao", "saldo-gestor"]),
    ("Jogadores", ["perfil"]),
    ("Ajuda e admin", ["comandos", "sync"]),
]


def _permissoes_faltando(guild):
    me = guild.me if guild else None
    if not me:
        return []
    perms = me.guild_permissions
    if perms.administrator:
        return []
    return [nome for chave, nome in PERMISSOES_NECESSARIAS.items() if not getattr(perms, chave, False)]


def _linhas_comando(cmd):
    """Uma linha por comando (grupos viram uma linha por subcomando)."""
    if isinstance(cmd, app_commands.Group):
        return [f"`/{cmd.name} {sub.name}` {sub.description}" for sub in cmd.commands]
    return [f"`/{cmd.name}` {cmd.description}"]


@bot.tree.command(name="comandos", description="Lista todos os comandos do bot e o que cada um faz")
async def comandos_cmd(interaction: discord.Interaction):
    todos = {c.name: c for c in bot.tree.get_commands()}
    usados = set()
    embed = discord.Embed(
        title="Comandos do bot",
        description=(
            "Os marcados com **[caller]**, **[gestor de saldo]** ou **[admin]** "
            "só funcionam pra quem tem essa função. Dá pra fazer quase tudo também pelo site, "
            f"no painel do servidor: {FRONTEND_URL_PUBLICO}"
        ),
        color=0xE2BC62,
    )
    for titulo, nomes in SECOES_COMANDOS:
        linhas = []
        for n in nomes:
            if n in todos:
                usados.add(n)
                linhas += _linhas_comando(todos[n])
        if linhas:
            embed.add_field(name=titulo, value="\n".join(linhas)[:1024], inline=False)
    resto = [ln for n, c in sorted(todos.items()) if n not in usados for ln in _linhas_comando(c)]
    if resto:
        embed.add_field(name="Outros", value="\n".join(resto)[:1024], inline=False)

    faltando = _permissoes_faltando(interaction.guild)
    if faltando:
        embed.add_field(
            name="⚠️ Faltam permissões pro bot",
            value="Dê ao cargo do bot: " + ", ".join(faltando) + ". Sem isso alguns comandos falham.",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)



@bot.event
async def on_guild_join(guild: discord.Guild):
    """Servidor novo: cria a pasta de dados (assim ele ja aparece no site pro
    admin logar) e manda uma mensagem de boas-vindas com o que falta configurar."""
    set_current_guild(guild.id)
    try:
        (GUILD_DATA_DIR / str(guild.id)).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[erro] nao criei a pasta da guilda {guild.id}: {e}")

    faltando = _permissoes_faltando(guild)
    texto = (
        f"Opa! O **{bot.user.name}** chegou no **{guild.name}**.\n\n"
        "• Use `/comandos` pra ver tudo o que o bot faz.\n"
        f"• Pelo site dá pra montar composições, builds e criar eventos: {FRONTEND_URL_PUBLICO} "
        "(entre com o Discord sendo admin deste servidor).\n"
        f"• Em Configurações do servidor → Cargos, arraste o cargo **{bot.user.name}** pra cima "
        "dos cargos de classe/função. O Discord não deixa o bot dar ou tirar um cargo que esteja acima do dele."
    )
    if faltando:
        texto += "\n\n⚠️ Faltam permissões: " + ", ".join(faltando) + ". O jeito mais fácil é remover e adicionar o bot de novo pelo link de convite."

    canal = guild.system_channel
    if not canal or not canal.permissions_for(guild.me).send_messages:
        canal = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
    if canal:
        try:
            await canal.send(texto)
            return
        except discord.HTTPException:
            pass
    if guild.owner:
        try:
            await guild.owner.send(texto)
        except discord.HTTPException:
            pass


bot.tree.add_command(classes_group)
bot.tree.add_command(evento_group)
bot.tree.add_command(comp_group)
bot.tree.add_command(saldo_group)
bot.tree.add_command(permissao_group)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    set_current_guild(interaction.guild_id)
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ Voce nao tem permissao pra usar esse comando (precisa de Gerenciar Cargos)."
    else:
        print(f"[erro] Comando falhou: {error!r}")
        msg = f"⚠️ Deu erro: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# =========================
# TRACKING DE PRESENCA
# =========================

@bot.event
async def on_voice_state_update(member, before, after):
    set_current_guild(member.guild.id)
    events = load_events()
    changed = False
    uid = str(member.id)

    for event_id, event in events.items():
        if event["status"] != "aberto" or uid not in event["participantes"]:
            continue

        canal_evento_id = event["canal_voz_id"]
        p = event["participantes"][uid]

        entrou_no_canal = bool(after.channel and after.channel.id == canal_evento_id)
        estava_no_canal = bool(before.channel and before.channel.id == canal_evento_id)

        # a contagem de tempo so comeca de verdade depois do organizador dar
        # /iniciar-evento (contagem_iniciada) - entrar na call antes disso so
        # garante a vaga/cargo, sem ligar o cronometro individual (evita
        # injustica de quem entra primeiro vs quem demora pra entrar).
        if entrou_no_canal and not estava_no_canal and not p.get("entrou_em") and event.get("contagem_iniciada"):
            p["entrou_em"] = time.time()
            changed = True
        elif estava_no_canal and not entrou_no_canal and p.get("entrou_em"):
            p["tempo_total"] += time.time() - p["entrou_em"]
            p["entrou_em"] = None
            changed = True
            for c in event["classes"]:
                if c["id"] == p["classe_id"] and c.get("role_id"):
                    role = member.guild.get_role(int(c["role_id"]))
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Saiu da call do evento")
                        except Exception:
                            pass

    if changed:
        save_events(events)

    # mesma logica, mas pra pings que ganharam sala propria (ver
    # _iniciar_sala_ping_core) - ping nao atribui cargo do Discord por
    # funcao, entao nao tem remocao de role aqui, so o tempo mesmo
    pings = load_pings()
    changed_pings = False
    for ping in pings.values():
        if ping.get("encerrado") or not ping.get("canal_voz_id"):
            continue
        participantes_tempo = ping.get("participantes_tempo") or {}
        if uid not in participantes_tempo:
            continue
        p = participantes_tempo[uid]
        canal_ping_id = ping["canal_voz_id"]
        entrou_no_canal = bool(after.channel and after.channel.id == canal_ping_id)
        estava_no_canal = bool(before.channel and before.channel.id == canal_ping_id)

        if entrou_no_canal and not estava_no_canal and not p.get("entrou_em") and ping.get("contagem_iniciada"):
            p["entrou_em"] = time.time()
            changed_pings = True
        elif estava_no_canal and not entrou_no_canal and p.get("entrou_em"):
            p["tempo_total"] += time.time() - p["entrou_em"]
            p["entrou_em"] = None
            changed_pings = True

    if changed_pings:
        save_pings(pings)


# =========================
# CALENDARIO / AGENDAMENTO (lembrete 5min + auto-inicio na hora)
# =========================

async def _post_lembrete_5min(event_id):
    events = load_events()
    event = events.get(event_id)
    if not event or event.get("alertado_5min") or event.get("status") != "agendado":
        return
    channel = bot.get_channel(event["canal_texto_id"])
    if channel:
        try:
            nomes = ", ".join(p["nome"] for p in event["participantes"].values()) or "ninguem confirmado ainda"
            await channel.send(f"\U0001F514 **{event['nome']}** comeca em 5 minutos! Inscritos: {nomes}")
        except Exception as e:
            print(f"[aviso] Falha ao enviar lembrete de {event_id}: {e}")
    event["alertado_5min"] = True
    save_events(events)


async def _auto_iniciar_evento(event_id):
    events = load_events()
    event = events.get(event_id)
    if not event or event.get("status") != "agendado":
        return

    guild = bot.get_guild(event.get("guild_id")) if event.get("guild_id") else None
    if not guild:
        channel_ref = bot.get_channel(event["canal_texto_id"])
        guild = channel_ref.guild if channel_ref else None
    if not guild:
        print(f"[erro] Nao achei o servidor pra auto-iniciar o evento {event_id}")
        return

    category = discord.utils.get(guild.categories, name=EVENTOS_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(EVENTOS_CATEGORY_NAME)
    voice_channel = await guild.create_voice_channel(f"\U0001F53A {event['nome']}", category=category)

    events = load_events()
    event = events.get(event_id)
    if not event or event.get("status") != "agendado":
        return
    event["canal_voz_id"] = voice_channel.id
    event["status"] = "aberto"
    save_events(events)

    await refresh_evento_panel(event_id)
    channel = bot.get_channel(event["canal_texto_id"])
    if channel:
        try:
            await channel.send(
                f"▶️ **{event['nome']}** comecou! Call: {voice_channel.mention} — clique de novo no seu slot pra eu te mover. "
                f"Quando todo mundo estiver na call, o organizador roda `/iniciar-evento` pra começar a contagem de tempo pra todos ao mesmo tempo."
            )
        except Exception:
            pass


@tasks.loop(seconds=60)
async def scheduler_loop():
    now = time.time()
    for guild_id in list_tenant_guild_ids():
        set_current_guild(guild_id)
        events = load_events()
        for event_id, event in list(events.items()):
            if event.get("status") != "agendado":
                continue
            quando = event.get("quando")
            if quando is None:
                continue
            if not event.get("alertado_5min") and now >= quando - 300:
                await _post_lembrete_5min(event_id)
            if now >= quando:
                await _auto_iniciar_evento(event_id)


@scheduler_loop.before_loop
async def _before_scheduler_loop():
    await bot.wait_until_ready()


# =========================
# STARTUP
# =========================

@bot.event
async def on_ready():
    print(f"[ok] Logado como {bot.user} ({bot.user.id})")

    for guild_id in list_tenant_guild_ids():
        set_current_guild(guild_id)

        events = load_events()
        for event_id, event in events.items():
            if event.get("status") in ("aberto", "agendado"):
                bot.add_view(EventoView(event_id))

        classes = load_classes()
        bot.add_view(FuncaoPingView(classes))
        bot.add_view(ClassesPainelView())
        bot.add_view(CompPainelView())

        pings = load_pings()
        for ping_key, ping in pings.items():
            if ping.get("encerrado"):
                bot.add_view(PingAvaliarView(ping_key))
            elif ping.get("ping_id"):
                # pings novos (com botao) tem ping_id; pings antigos (reacao) nao
                # tem view persistente pra restaurar - continuam funcionando via reacao.
                bot.add_view(PingFuncaoView(ping["ping_id"], ping["funcoes"]))

    global _internal_api_started
    if not _internal_api_started:
        _internal_api_started = True
        bot.loop.create_task(_start_internal_api())

    if not scheduler_loop.is_running():
        scheduler_loop.start()

    try:
        synced = await bot.tree.sync()
        print(f"[ok] {len(synced)} comandos sincronizados globalmente (pode levar ate 1h pra propagar; use /sync num servidor pra instantaneo).")
    except Exception as e:
        print(f"[erro] Falha ao sincronizar comandos: {e}")



# =========================
# API INTERNA (localhost) - ponte pro painel do site criar/editar/encerrar
# pings e ver canais reais do Discord, sem precisar abrir o Discord. So
# escuta em 127.0.0.1 (nao exposta na internet) e exige um header secreto
# (INTERNAL_API_SECRET, mesmo .env do eventsite) como segunda trava.
# =========================

from aiohttp import web as _iweb

INTERNAL_API_PORT = int(os.environ.get("INTERNAL_API_PORT", "8100"))
INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET", "")
_internal_api_started = False


def _internal_check(request):
    if INTERNAL_API_SECRET and request.headers.get("X-Internal-Secret") != INTERNAL_API_SECRET:
        raise _iweb.HTTPForbidden(text="secret invalido")


async def _internal_channels(request):
    _internal_check(request)
    try:
        guild_id = int(request.query.get("guild_id", "0"))
    except ValueError:
        return _iweb.json_response({"error": "guild_id invalido"}, status=400)
    guild = bot.get_guild(guild_id)
    if not guild:
        return _iweb.json_response({"error": "guilda nao encontrada (bot nao esta nela?)"}, status=404)
    canais = [
        {"id": str(c.id), "name": c.name, "category": c.category.name if c.category else None}
        for c in guild.text_channels
        if c.permissions_for(guild.me).send_messages
    ]
    return _iweb.json_response({"channels": canais})


def _guild_da_query(request):
    try:
        return bot.get_guild(int(request.query.get("guild_id", "0")))
    except ValueError:
        return None


async def _internal_nivel(request):
    """Nivel de permissao de uma pessoa num servidor (admin/gestor/caller/None),
    calculado com os cargos reais dela. O site usa isso pra liberar telas."""
    _internal_check(request)
    guild = _guild_da_query(request)
    if not guild:
        return _iweb.json_response({"nivel": None, "no_servidor": False})
    try:
        uid = int(request.query.get("user_id", "0"))
    except ValueError:
        return _iweb.json_response({"error": "user_id invalido"}, status=400)
    membro = guild.get_member(uid)
    if membro is None:
        try:
            membro = await guild.fetch_member(uid)
        except discord.HTTPException:
            return _iweb.json_response({"nivel": None, "no_servidor": False})
    set_current_guild(guild.id)
    nivel = _nivel(membro, guild)
    if nivel is None and membro.guild_permissions.manage_roles:
        nivel = "caller"
    return _iweb.json_response({"nivel": nivel, "no_servidor": True, "nome": membro.display_name})


async def _internal_membros(request):
    _internal_check(request)
    guild = _guild_da_query(request)
    if not guild:
        return _iweb.json_response({"error": "guilda nao encontrada"}, status=404)
    q = (request.query.get("q") or "").strip().lower()
    ids = [x for x in (request.query.get("ids") or "").split(",") if x.strip().isdigit()]
    if ids:
        achados = [guild.get_member(int(i)) for i in ids]
    else:
        achados = [m for m in guild.members if not m.bot and (
            not q or q in m.display_name.lower() or q in m.name.lower())]
        achados.sort(key=lambda m: m.display_name.lower())
        achados = achados[:25]
    return _iweb.json_response({"membros": [
        {"id": str(m.id), "nome": m.display_name, "usuario": m.name,
         "avatar": str(m.display_avatar.url) if m.display_avatar else None}
        for m in achados if m is not None
    ]})


async def _internal_cargos(request):
    _internal_check(request)
    guild = _guild_da_query(request)
    if not guild:
        return _iweb.json_response({"error": "guilda nao encontrada"}, status=404)
    cargos = [
        {"id": str(r.id), "nome": r.name, "cor": f"#{r.colour.value:06x}" if r.colour.value else None}
        for r in sorted(guild.roles, key=lambda r: -r.position)
        if not r.is_default() and not r.managed
    ]
    return _iweb.json_response({"cargos": cargos})


async def _internal_create_ping(request):
    _internal_check(request)
    data = await request.json()
    try:
        guild_id = int(data["guild_id"])
        canal_id = int(data["canal_id"])
    except (KeyError, ValueError, TypeError):
        return _iweb.json_response({"error": "guild_id/canal_id invalido"}, status=400)

    guild = bot.get_guild(guild_id)
    if not guild:
        return _iweb.json_response({"error": "guilda nao encontrada (bot nao esta nela?)"}, status=404)
    canal = guild.get_channel(canal_id)
    if not canal:
        return _iweb.json_response({"error": "canal nao encontrado"}, status=404)

    templates = load_templates(guild_id)
    comp = next((t for t in templates if t["id"] == data.get("composicao_id")), None)
    if not comp:
        return _iweb.json_response({"error": "composicao nao encontrada"}, status=404)
    if not comp.get("slots"):
        return _iweb.json_response({"error": "essa composicao nao tem slots/builds ainda"}, status=400)

    funcoes = _parse_slots_texto(str(data.get("vagas_texto") or ""))
    if not funcoes:
        return _iweb.json_response({"error": "nenhuma vaga entendida - uma funcao por linha"}, status=400)
    funcoes = _assign_emojis(funcoes)
    for f in funcoes:
        f["claimed"] = []
        f["extra"] = []
        f["build_ref"] = _match_build_ref(f["nome"], comp)

    titulo = (data.get("titulo") or "").strip() or comp["nome"]
    local = (data.get("local") or "").strip()
    fee = (data.get("fee") or "").strip()

    quando = None
    horario_raw = (data.get("horario") or "").strip()
    if horario_raw:
        partes = horario_raw.split()
        if len(partes) != 2:
            return _iweb.json_response({"error": "horario invalido - use DD/MM HH:MM"}, status=400)
        try:
            dt = _parse_data_hora(partes[0], partes[1])
        except (ValueError, IndexError):
            return _iweb.json_response({"error": "horario invalido - use DD/MM HH:MM"}, status=400)
        quando = dt.timestamp()

    criado_por = int(data.get("criado_por_discord_id") or 0)
    criado_por_nome = data.get("criado_por_nome") or "Painel do site"

    try:
        img_bytes = await asyncio.to_thread(render_comp_card, comp)
    except Exception as e:
        return _iweb.json_response({"error": f"erro gerando imagem da build: {e}"}, status=500)

    try:
        thread = await canal.create_thread(
            name=f"📌 {titulo}"[:100], type=discord.ChannelType.public_thread, auto_archive_duration=1440,
        )
    except discord.Forbidden:
        return _iweb.json_response({"error": "sem permissao pra criar topico nesse canal"}, status=403)

    ping_id = str(uuid.uuid4())
    ping = {
        "ping_id": ping_id,
        "guild_id": guild_id, "canal_id": canal.id, "thread_id": thread.id,
        "mensagem_id": None, "titulo": titulo, "composicao_id": comp["id"],
        "local": local, "fee": fee, "quando": quando,
        "criado_por": criado_por, "criado_por_nome": criado_por_nome,
        "criado_em": time.time(), "funcoes": funcoes,
        "banco": [], "ausentes": [],
        "canal_voz_id": None, "contagem_iniciada": False, "participantes_tempo": {},
    }

    await create_live_event(ping)

    embed = _build_ping_embed(ping)
    file = discord.File(io.BytesIO(img_bytes), filename="build.png")
    embed.set_image(url="attachment://build.png")
    view = PingFuncaoView(ping_id, funcoes)
    mencao = f"<@{criado_por}>" if criado_por else criado_por_nome
    copy = (
        f"📢 {titulo} Está Convocando você! Criado por {criado_por_nome} pelo painel do site. "
        f"Clique no botão da sua função aqui embaixo, é por ordem de chegada, "
        f"então corre antes que lote! Bora fechar esse grupo! 🔥"
    )
    msg = await thread.send(content=copy, embed=embed, file=file, view=view)
    bot.add_view(view)

    # a build de cada funcao nao vai mais publica no topico - ela e enviada
    # no privado de quem clicar em Confirmar (ver handle_ping_button)

    ping["mensagem_id"] = msg.id
    pings = load_pings(guild_id)
    pings[ping_id] = ping
    save_pings(pings, guild_id)

    return _iweb.json_response({"ping_id": ping_id, "thread_id": str(thread.id)})


async def _internal_edit_ping(request):
    _internal_check(request)
    ping_id = request.match_info["ping_id"]
    data = await request.json()
    try:
        guild_id = int(data["guild_id"])
    except (KeyError, ValueError, TypeError):
        return _iweb.json_response({"error": "guild_id invalido"}, status=400)

    pings = load_pings(guild_id)
    ping = pings.get(ping_id)
    if not ping:
        return _iweb.json_response({"error": "ping nao encontrado"}, status=404)
    if ping.get("encerrado"):
        return _iweb.json_response({"error": "esse ping ja foi encerrado"}, status=400)

    if "titulo" in data and str(data["titulo"]).strip():
        ping["titulo"] = str(data["titulo"]).strip()[:80]
    if "local" in data:
        ping["local"] = str(data["local"] or "").strip()[:60]
    if "fee" in data:
        ping["fee"] = str(data["fee"] or "").strip()[:40]
    if "horario" in data:
        horario_raw = str(data["horario"] or "").strip()
        if not horario_raw:
            ping["quando"] = None
        else:
            partes = horario_raw.split()
            if len(partes) != 2:
                return _iweb.json_response({"error": "horario invalido - use DD/MM HH:MM"}, status=400)
            try:
                dt = _parse_data_hora(partes[0], partes[1])
            except (ValueError, IndexError):
                return _iweb.json_response({"error": "horario invalido - use DD/MM HH:MM"}, status=400)
            ping["quando"] = dt.timestamp()
    if "vagas" in data and isinstance(data["vagas"], list):
        por_nome = {f["nome"]: f for f in ping["funcoes"]}
        for item in data["vagas"]:
            nome = str(item.get("nome") or "")
            f = por_nome.get(nome)
            if not f:
                continue
            try:
                nova_vaga = max(0, int(item.get("vagas")))
            except (TypeError, ValueError):
                continue
            if nova_vaga < len(f["claimed"]):
                return _iweb.json_response(
                    {"error": f"'{nome}' ja tem {len(f['claimed'])} confirmados - nao da pra reduzir pra {nova_vaga}"},
                    status=400,
                )
            f["vagas"] = nova_vaga

    pings[ping_id] = ping
    save_pings(pings, guild_id)

    guild = bot.get_guild(guild_id)
    channel = guild.get_channel_or_thread(ping["thread_id"]) if guild else None
    if channel:
        try:
            message = await channel.fetch_message(ping["mensagem_id"])
            await message.edit(embed=_build_ping_embed(ping), view=PingFuncaoView(ping_id, ping["funcoes"]))
        except discord.HTTPException:
            pass

    return _iweb.json_response({"ok": True})


async def _internal_encerrar_ping(request):
    _internal_check(request)
    ping_id = request.match_info["ping_id"]
    data = await request.json()
    try:
        guild_id = int(data["guild_id"])
    except (KeyError, ValueError, TypeError):
        return _iweb.json_response({"error": "guild_id invalido"}, status=400)

    pings = load_pings(guild_id)
    alvo = pings.get(ping_id)
    if not alvo:
        return _iweb.json_response({"error": "ping nao encontrado"}, status=404)
    if alvo.get("encerrado"):
        return _iweb.json_response({"error": "esse ping ja foi encerrado"}, status=400)

    alvo["encerrado"] = True
    pings[ping_id] = alvo
    save_pings(pings, guild_id)

    await complete_live_event(alvo)

    guild = bot.get_guild(guild_id)
    thread = guild.get_channel_or_thread(alvo["thread_id"]) if guild else None
    if thread:
        if alvo.get("mensagem_id"):
            try:
                painel_msg = await thread.fetch_message(alvo["mensagem_id"])
                await painel_msg.edit(view=None)
            except discord.HTTPException:
                pass
        total_confirmados = sum(len(f["claimed"]) for f in alvo["funcoes"])
        resumo = "\n".join(
            f"{f['emoji']} **{f['nome']}**: " + (" ".join(f"<@{uid}>" for uid in f["claimed"]) or "ninguém")
            for f in alvo["funcoes"]
        )
        embed = discord.Embed(
            title=f"🔒 Ping encerrado · {alvo['titulo']}",
            description=f"Roster final ({total_confirmados} confirmados). As vagas estão travadas (encerrado pelo painel do site):\n\n{resumo}",
            color=0xE74C3C,
        )
        embed.set_footer(text="Clique em ⭐ pra avaliar o caller e quem jogou com você")
        try:
            await thread.send(embed=embed, view=PingAvaliarView(ping_id))
            bot.add_view(PingAvaliarView(ping_id))
        except discord.HTTPException:
            pass

    return _iweb.json_response({"ok": True})


async def _internal_split_ping(request):
    _internal_check(request)
    ping_id = request.match_info["ping_id"]
    data = await request.json()
    try:
        guild_id = int(data["guild_id"])
        profit = float(data["profit"])
        reparo = float(data.get("reparo") or 0)
    except (KeyError, ValueError, TypeError):
        return _iweb.json_response({"error": "guild_id/profit/reparo invalido"}, status=400)
    taxa_pct = None
    if data.get("taxa_pct") not in (None, ""):
        try:
            taxa_pct = float(data.get("taxa_pct"))
        except (ValueError, TypeError):
            return _iweb.json_response({"error": "taxa_pct invalida"}, status=400)
    modo = data.get("modo")
    if modo not in ("dividir", "comprador", "saldar"):
        return _iweb.json_response({"error": "modo invalido"}, status=400)

    pings = load_pings(guild_id)
    ping = pings.get(ping_id)
    if not ping:
        return _iweb.json_response({"error": "ping nao encontrado"}, status=404)
    if not ping.get("encerrado"):
        return _iweb.json_response({"error": "encerre o ping antes de dividir o loot"}, status=400)
    if ping.get("loot_dividido"):
        return _iweb.json_response({"error": "esse ping ja teve o loot dividido"}, status=400)

    tempos, _por_tempo = _ping_participantes_tempo(ping)
    if not tempos:
        return _iweb.json_response({"error": "ninguem confirmou presenca nesse ping - nao da pra dividir"}, status=400)

    guild = bot.get_guild(guild_id)
    thread = guild.get_channel_or_thread(ping["thread_id"]) if guild else None

    if modo == "saldar":
        # mesma logica do botao "Saldar" no Discord: se a guild vendeu o loot
        # e paga em prata, reparo/taxa_pct (ja parseados acima) descontam
        # antes de creditar o saldo de cada um
        tem_desconto = bool(reparo) or bool(taxa_pct)
        liquido, shares = compute_split(tempos, profit, has_buyer=tem_desconto, reparo=reparo, taxa_pct=taxa_pct)
        linhas = []
        for uid, s in sorted(shares.items(), key=lambda kv: kv[1]["valor"], reverse=True):
            nome = await _resolver_nome(guild, uid)
            novo_saldo = add_saldo(guild_id, uid, nome, s["valor"], motivo=f"Loot: {ping['titulo']}")
            linhas.append(f"{nome:<20} +{_fmt_prata(s['valor']):>14} prata   (saldo: {_fmt_prata(novo_saldo)})")
        aviso_desconto = f"\nTaxa de {(taxa_pct or 0):.0f}% + reparo de {_fmt_prata(reparo)} prata descontados antes de creditar." if tem_desconto else ""
        embed = discord.Embed(
            title=f"💾 Loot saldado · {ping['titulo']}",
            description=(
                f"Profit: {_fmt_prata(profit)} prata{aviso_desconto}\nValor creditado: **{_fmt_prata(liquido)} prata**\n\n"
                "```\n" + "\n".join(linhas) + "\n```\nCada um pode conferir com `/saldo membro`. Ninguem precisa pagar agora."
            ),
            color=0x5865F2,
        )
    else:
        has_buyer = modo == "comprador"
        liquido, shares = compute_split(tempos, profit, has_buyer, reparo, taxa_pct)
        linhas = []
        for uid, s in sorted(shares.items(), key=lambda kv: kv[1]["valor"], reverse=True):
            nome = await _resolver_nome(guild, uid)
            linhas.append(f"{nome:<20} {s['pct']*100:5.1f}%   {_fmt_prata(s['valor']):>14} prata")
        taxa_exibida = taxa_pct if taxa_pct is not None else BUYER_FEE * 100
        aviso_taxa = f"\nTaxa de {taxa_exibida:.0f}% descontada do valor (reparo: {_fmt_prata(reparo)} prata)." if has_buyer else ""
        embed = discord.Embed(
            title=f"💰 Divisão do loot · {ping['titulo']}",
            description=(
                f"Profit bruto: {_fmt_prata(profit)} prata{aviso_taxa}\nValor a dividir: **{_fmt_prata(liquido)} prata**\n\n"
                "```\n" + "\n".join(linhas) + "\n```"
            ),
            color=0x2ECC71,
        )
        if has_buyer:
            embed.set_footer(text="O comprador paga o valor acima pra cada pessoa (exceto pra si mesmo).")

    embed.timestamp = discord.utils.utcnow()
    ping["loot_dividido"] = True
    ping["loot_profit"] = profit
    ping["loot_liquido"] = liquido
    pings[ping_id] = ping
    save_pings(pings, guild_id)

    if thread:
        try:
            await thread.send(content="📋 Divisão feita pelo painel do site:", embed=embed)
        except discord.HTTPException:
            pass

    return _iweb.json_response({"ok": True, "liquido": liquido})


async def _start_internal_api():
    app = _iweb.Application()
    app.add_routes([
        _iweb.get("/channels", _internal_channels),
        _iweb.get("/nivel", _internal_nivel),
        _iweb.get("/membros", _internal_membros),
        _iweb.get("/cargos", _internal_cargos),
        _iweb.post("/pings", _internal_create_ping),
        _iweb.put("/pings/{ping_id}", _internal_edit_ping),
        _iweb.post("/pings/{ping_id}/encerrar", _internal_encerrar_ping),
        _iweb.post("/pings/{ping_id}/split", _internal_split_ping),
    ])
    runner = _iweb.AppRunner(app)
    await runner.setup()
    site = _iweb.TCPSite(runner, "127.0.0.1", INTERNAL_API_PORT)
    await site.start()
    print(f"[ok] API interna (painel do site) ouvindo em 127.0.0.1:{INTERNAL_API_PORT}")


if __name__ == "__main__":
    bot.run(TOKEN)
