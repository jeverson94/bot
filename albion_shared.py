"""
Dados e helpers compartilhados entre o bot do Discord (event_bot.py) e o site
de configuracao (eventsite.py). Nenhum dos dois arquivos duplica essa logica -
os dois leem/escrevem os MESMOS arquivos JSON nessa pasta, entao uma alteracao
feita no site aparece no bot (e vice-versa) sem precisar sincronizar nada.

Esse modulo nao depende de discord.py nem de Flask - so stdlib + requests.
"""

import contextvars
import json
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_DIR = Path(__file__).parent
ITEMS_DB_FILE = BASE_DIR / "items_db.json"  # catalogo de itens do Albion - global, nao e por guilda

# ---------------------------------------------------------------------------
# Multi-tenant: cada guilda (servidor Discord) tem sua propria subpasta em
# guild_data/<guild_id>/ com os mesmos nomes de arquivo de antes.
#
# Como o guild_id chega em cada funcao load_*/save_*:
#   1. Passado explicitamente (guild_id="123...") - sempre vence, usado pelo
#      eventsite.py que resolve o guild_id a partir da sessao logada.
#   2. Se nao passado (None, o novo default), cai pro "contexto ambiente":
#      um contextvars.ContextVar setado uma unica vez, no TOPO de cada
#      comando/callback/evento do event_bot.py, via set_current_guild(...).
#      Dai em diante qualquer load_/save_* chamado (direto ou aninhado, nao
#      importa quantas funcoes de distancia) enxerga o guild certo sozinho -
#      nao precisa passar guild_id por 90 call sites manualmente.
#   3. Se nem isso (nenhum contexto setado - ex. script solto), cai pro
#      DEFAULT_GUILD_ID (guilda STOMP, a original, pre-multi-tenant).
# ---------------------------------------------------------------------------
GUILD_DATA_DIR = BASE_DIR / "guild_data"
DEFAULT_GUILD_ID = "576409638031851541"  # guilda STOMP (unica ate hoje) - fallback final

# Espaco pessoal: quem loga mas nao administra nenhuma guilda com o bot
# instalado ganha um tenant proprio, chaveado pelo discord_id dele (nao por
# um guild_id de verdade) - reaproveita 100% da mesma infra de dados (mesma
# pasta, mesmos load_/save_*), so troca a chave. NUNCA e uma guilda real -
# o bot ignora esses IDs (ver list_tenant_guild_ids abaixo), ja que nao ha
# servidor/canal/cargo nenhum por tras - so builds/composicoes/roster como
# planejamento pessoal.
PERSONAL_TENANT_PREFIX = "user_"


def personal_tenant_id(discord_user_id):
    return f"{PERSONAL_TENANT_PREFIX}{discord_user_id}"


def is_personal_tenant(guild_id):
    return str(guild_id).startswith(PERSONAL_TENANT_PREFIX)


# ---------------------------------------------------------------------------
# Slots: unidade de acesso comprada (por enquanto, "comprada" e provisorio -
# ver source="self_serve_free" em eventsite.py - vira compra de verdade
# quando o Stripe entrar). Cada slot da acesso a UM workspace:
#   - type="guild"    -> workspace_id = guild_id real (ex: "576409...").
#                        1 slot de guild libera o bot pra guilda INTEIRA,
#                        nao so pra quem comprou.
#   - type="personal" -> workspace_id = "user_<slot_id>" (gerado na hora).
#                        Uma pessoa pode ter varios (cada slot pessoal = um
#                        espaco isolado a mais, tipo "Pessoal 1", "Pessoal 2").
# Fica num arquivo separado (nao dentro de guild_data/) porque e um dado de
# CONTA (quem comprou o que), nao um dado de workspace.
# ---------------------------------------------------------------------------
SLOTS_FILE = BASE_DIR / "slots.json"


def load_slots():
    return _load_json(SLOTS_FILE, {"slots": []}).get("slots", [])


def save_slots(slots):
    _save_json(SLOTS_FILE, {"slots": slots})


def slots_for_user(discord_id):
    """Slots ativos comprados por esse discord_id (dono do slot - nao tem
    nada a ver com quem administra a guilda no Discord, so com quem pagou)."""
    discord_id = str(discord_id)
    return [s for s in load_slots() if str(s.get("owner_discord_id")) == discord_id and s.get("status", "active") == "active"]


def all_slots_for_user(discord_id):
    """Todos os slots desse discord_id, qualquer status (ativo, cancelado,
    past_due) - usado na pagina de Cobranca pra mostrar o historico completo,
    nao so o que ta ativo agora."""
    discord_id = str(discord_id)
    return [s for s in load_slots() if str(s.get("owner_discord_id")) == discord_id]


def stripe_customer_id_for_user(discord_id):
    """Acha um stripe_customer_id ja usado por esse discord_id (de qualquer
    slot, ativo ou nao) pra reaproveitar no proximo checkout - assim a pessoa
    nao vira 3 clientes Stripe diferentes por ter comprado 3 slots. Pega o
    mais recente (created_at) se tiver mais de um."""
    candidatos = [s for s in all_slots_for_user(discord_id) if s.get("stripe_customer_id")]
    if not candidatos:
        return None
    candidatos.sort(key=lambda s: s.get("created_at", 0), reverse=True)
    return candidatos[0]["stripe_customer_id"]


_current_guild_id = contextvars.ContextVar("current_guild_id", default=None)


def set_current_guild(guild_id):
    """Define a guilda 'ambiente' do contexto async atual (e de tudo que ele
    chamar/awaitar a partir daqui). Chamar UMA vez no topo de cada comando
    slash, callback de botao/modal, evento (on_raw_reaction_add etc.) do bot,
    antes de qualquer load_/save_*. Retorna o token (nao precisa usar, mas
    fica disponivel caso algum dia seja preciso um contextvars.reset())."""
    return _current_guild_id.set(str(guild_id) if guild_id else None)


def get_current_guild():
    """Guilda do contexto ambiente atual, ou DEFAULT_GUILD_ID se nada foi
    setado (compat com codigo antigo/scripts soltos)."""
    return _current_guild_id.get() or DEFAULT_GUILD_ID


def _guild_dir(guild_id):
    resolved = str(guild_id) if guild_id else get_current_guild()
    d = GUILD_DATA_DIR / resolved
    d.mkdir(parents=True, exist_ok=True)
    return d


def _guild_file(guild_id, filename):
    return _guild_dir(guild_id) / filename


def list_tenant_guild_ids():
    """Todos os guild_id de guildas REAIS que ja tem pasta em guild_data/
    (ou seja, toda guilda que ja usou o bot pelo menos uma vez). Usado pelo
    scheduler_loop e pelo on_ready do BOT pra processar/re-registrar views de
    TODAS as guildas, nao so a que estiver no contexto ambiente no momento.
    So numeros (snowflake do Discord) - filtra fora os espacos pessoais
    (user_<id>), que nao sao guildas de verdade (sem servidor/canal por
    tras) e o bot nunca deve tentar tratar como uma."""
    if not GUILD_DATA_DIR.exists():
        return [DEFAULT_GUILD_ID]
    ids = [d.name for d in GUILD_DATA_DIR.iterdir() if d.is_dir() and d.name.isdigit()]
    return ids or [DEFAULT_GUILD_ID]


GUILD_TAG = "S T O M P"
API_SEARCH = "https://gameinfo.albiononline.com/api/gameinfo/search"
RENDER_BASE = "https://render.albiononline.com/v1/item"

BUYER_FEE = 0.15  # taxa de 15% descontada do valor quando ha comprador do loot
GOLD_HEX = "#E2BC62"
GOLD = 0xE2BC62
TIMEZONE = ZoneInfo("America/Sao_Paulo")


# =========================
# JSON HELPERS
# =========================

def _load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _slug(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9_]", "_", text)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    return (text or "item")[:32]


def _fmt_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_prata(n):
    return f"{n:,.0f}".replace(",", ".")


def _looks_like_item_id(texto):
    return bool(re.match(r"^T\d+_", texto.strip().upper()))


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


def _parse_builds_texto(builds_texto):
    """Aceita 'T8_2H_HALBERD, Martelo de Guerra' (formato antigo, digitado a mao) OU
    'T8_2H_HALBERD::Alabarda, T4_2H_HAMMER::Martelo de Guerra' (formato novo, vindo do
    seletor visual - id::nome ja resolvido, sem precisar bater na API de busca)."""
    builds_list = []
    for raw in builds_texto.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if "::" in raw:
            item_id, nome = raw.split("::", 1)
            item_id, nome = item_id.strip(), nome.strip()
            builds_list.append({"nome": nome or item_id, "item_id": item_id, "arma_nome": None, "emoji_id": None})
        elif _looks_like_item_id(raw):
            builds_list.append({"nome": raw, "item_id": raw, "arma_nome": None, "emoji_id": None})
        else:
            builds_list.append({"nome": raw, "item_id": None, "arma_nome": raw, "emoji_id": None})
    return builds_list


def _parse_single_build_field(raw):
    """Mesma ideia de _parse_builds_texto mas pra campo de arma unica (roster de
    classes). Devolve (item_id, nome) - nome pode precisar resolver via API ainda
    se vier so um nome digitado a mao (fallback antigo, sem o seletor visual)."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if "::" in raw:
        item_id, nome = raw.split("::", 1)
        return item_id.strip(), nome.strip()
    if _looks_like_item_id(raw):
        return raw, raw
    return None, raw


def _parse_data_hora(data_str, hora_str):
    """'DD/MM' ou 'DD/MM/AAAA' + 'HH:MM' -> datetime com timezone de Brasilia."""
    partes = data_str.strip().split("/")
    if len(partes) == 2:
        dia, mes = int(partes[0]), int(partes[1])
        ano = datetime.now(TIMEZONE).year
    elif len(partes) == 3:
        dia, mes, ano = int(partes[0]), int(partes[1]), int(partes[2])
        if ano < 100:
            ano += 2000
    else:
        raise ValueError("formato de data invalido")
    h, m = [int(x) for x in hora_str.strip().split(":")]
    return datetime(ano, mes, dia, h, m, tzinfo=TIMEZONE)


# =========================
# EQUIPAMENTO (paperdoll de build - 9 quadrados, igual jogo)
# =========================

EQUIP_SLOTS = [
    {"key": "bag", "label": "Bolsa"},
    {"key": "head", "label": "Elmo"},
    {"key": "cape", "label": "Capa"},
    {"key": "mainhand", "label": "Arma"},
    {"key": "chest", "label": "Peitoral"},
    {"key": "offhand", "label": "Off-Hand"},
    {"key": "potion", "label": "Pocao"},
    {"key": "shoes", "label": "Botas"},
    {"key": "food", "label": "Comida"},
    {"key": "mount", "label": "Montaria", "own_row": True},
]


def load_items_db():
    """Catalogo de itens pro seletor visual (paperdoll), agrupado por slot.
    Cada entrada: {"id": "T4_...", "nome": "..."}. Gerado uma vez e verificado
    contra o CDN de render do Albion (ver build_items_db.py / verify_items.py)."""
    return _load_json(ITEMS_DB_FILE, {})


def get_build_equip(build):
    """Devolve o loadout completo (9 slots) de uma build. Cada slot vira sempre
    {"item": id_ou_None, "swaps": [ids]} - suporta 3 formatos salvos:
    - novo: {"item":.., "swaps":[..]}
    - antigo (antes dos swaps): so uma string com o item_id
    - bem antigo (antes do paperdoll): 'equip' vazio, so 'item_id' solto na build
      (nesse caso vira a mainhand automaticamente)."""
    raw = dict(build.get("equip") or {})
    if not raw and build.get("item_id"):
        raw = {"mainhand": build["item_id"]}
    equip = {}
    for key, val in raw.items():
        if isinstance(val, dict):
            item = val.get("item")
            swaps = [s for s in (val.get("swaps") or []) if s]
            if item or swaps:
                equip[key] = {"item": item, "swaps": swaps}
        elif val:
            equip[key] = {"item": val, "swaps": []}
    return equip


def set_build_equip(build, equip):
    """Salva o loadout (item principal + swaps por quadrado) na build e mantem
    'item_id' (usado pro icone do botao no Discord) sincronizado com a arma
    principal da mao principal (swaps nao contam pro icone)."""
    clean = {}
    for key, val in equip.items():
        if isinstance(val, dict):
            item = val.get("item") or None
            swaps = [s for s in (val.get("swaps") or []) if s]
        else:
            item, swaps = (val or None), []
        if item or swaps:
            clean[key] = {"item": item, "swaps": swaps}
    build["equip"] = clean
    build["item_id"] = (clean.get("mainhand") or {}).get("item")


# =========================
# HABILIDADES POR ITEM (catalogo pra mostrar Q/W/E/D/R/F/P1/P2 de cada peca,
# igual o "detalhes do item" do site de referencia albionzvz.com)
# =========================
# Chave = "familia" do item (item_id sem o prefixo de tier "T4_"/"T8_" e sem o
# sufixo de encantamento "@1"/"@2" - a familia e o que define as habilidades;
# tier/encantamento so mudam o poder/IP, nao o nome das habilidades).
# Ainda nao cobre TODO item do jogo (sao milhares) - cobre por enquanto as
# armas/pecas que ja aparecem nas composicoes reais (STOMP e os modelos
# publicos do Empório). Dá pra ir completando aos poucos: quando um item nao
# tiver entrada aqui, get_item_abilities() devolve None e quem chamou trata
# como "ainda nao catalogado" (nao quebra nada, so nao mostra as habilidades).

ITEM_ABILITIES = {
    # ---- armas (mainhand) ----
    "2H_HAMMER": {"tipo": "mainhand", "habilidades": [
        {"slot": "Q", "nome": "Pancada Poderosa"},
        {"slot": "W", "nome": "Gêiser de Poder"},
        {"slot": "E", "nome": "Enfrentamento"},
        {"slot": "P1", "nome": "Golpe Atordoante"},
    ]},
    "MAIN_MACE": {"tipo": "mainhand", "habilidades": [
        {"slot": "Q", "nome": "Batida defensiva"},
        {"slot": "W", "nome": "Quebra Chão"},
        {"slot": "E", "nome": "Salto Profundo"},
        {"slot": "P1", "nome": "Golpe Atordoante"},
    ]},
    "2H_QUARTERSTAFF": {"tipo": "mainhand", "habilidades": [
        {"slot": "Q", "nome": "Combo Concussivo"},
        {"slot": "W", "nome": "Dança do Vento"},
        {"slot": "E", "nome": "Salto Seguro"},
        {"slot": "P1", "nome": "Golpe Atordoante"},
    ]},
    "2H_ENIGMATICSTAFF": {"tipo": "mainhand", "habilidades": [
        {"slot": "Q", "nome": "Míssil Encadeado"},
        {"slot": "W", "nome": "Lâmina Enigmática"},
        {"slot": "E", "nome": "Raio Protetor"},
        {"slot": "P1", "nome": "Poder Remanescente"},
    ]},
    "2H_HOLYSTAFF": {"tipo": "mainhand", "habilidades": [
        {"slot": "Q", "nome": "Cura Generosa"},
        {"slot": "W", "nome": "Pulsação Sagrada"},
        {"slot": "E", "nome": "Explosão Sagrada"},
        {"slot": "P1", "nome": "Caridade Intensificada"},
    ]},
    "2H_LONGBOW": {"tipo": "mainhand", "habilidades": [
        {"slot": "Q", "nome": "Multidisparo"},
        {"slot": "W", "nome": "Raio de Luz"},
        {"slot": "E", "nome": "Chuva de Flechas"},
        {"slot": "P1", "nome": "Veneno Desacelerador"},
    ]},
    # ---- cabecas (head) ----
    "HEAD_PLATE_HELL": {"tipo": "head", "habilidades": [
        {"slot": "D", "nome": "Silenciar"},
        {"slot": "P1", "nome": "Resistência"},
    ]},
    "HEAD_PLATE_SET3": {"tipo": "head", "habilidades": [
        {"slot": "D", "nome": "Escudo Energizante"},
        {"slot": "P1", "nome": "Resistência"},
    ]},
    "HEAD_LEATHER_SET2": {"tipo": "head", "habilidades": [
        {"slot": "D", "nome": "Escudo Energizante"},
        {"slot": "P1", "nome": "Mente Equilibrada"},
    ]},
    "HEAD_CLOTH_SET2": {"tipo": "head", "habilidades": [
        {"slot": "D", "nome": "Escudo Energizante"},
        {"slot": "P1", "nome": "Agressão"},
    ]},
    "HEAD_CLOTH_SET1": {"tipo": "head", "habilidades": [
        {"slot": "D", "nome": "Escudo Energizante"},
        {"slot": "P1", "nome": "Agressão"},
    ]},
    # ---- peitorais (chest) ----
    # OBS: "Armadura de Cavaleiro do Ancião" (ARMOR_PLATE_SET2) apareceu com o
    # R diferente em duas builds de referencia (Parede de Vento numa, Cicatrização
    # noutra) - fica como "a confirmar" ate verificar qual e a correta.
    "ARMOR_PLATE_SET2": {"tipo": "chest", "habilidades": [
        {"slot": "R", "nome": "(a confirmar - viu 'Parede de Vento' e 'Cicatrização' em builds diferentes)"},
        {"slot": "P1", "nome": "Resistência"},
        {"slot": "P2", "nome": "Esmagamento de Espírito"},
    ]},
    "ARMOR_PLATE_SET3": {"tipo": "chest", "habilidades": [
        {"slot": "R", "nome": "Cicatrização"},
        {"slot": "P1", "nome": "Resistência"},
        {"slot": "P2", "nome": "Esmagamento de Espírito"},
    ]},
    "ARMOR_CLOTH_SET2": {"tipo": "chest", "habilidades": [
        {"slot": "R", "nome": "Cicatrização"},
        {"slot": "P1", "nome": "Agressão"},
    ]},
    "ARMOR_LEATHER_SET3": {"tipo": "chest", "habilidades": [
        {"slot": "R", "nome": "Cicatrização"},
        {"slot": "P1", "nome": "Mente Equilibrada"},
    ]},
    # ---- sapatos (shoes) ----
    "SHOES_LEATHER_ROYAL": {"tipo": "shoes", "habilidades": [
        {"slot": "F", "nome": "Esquiva"},
        {"slot": "P1", "nome": "Mensageiro"},
    ]},
    "SHOES_PLATE_SET3": {"tipo": "shoes", "habilidades": [
        {"slot": "F", "nome": "Esquiva"},
        {"slot": "P1", "nome": "Mensageiro"},
    ]},
    "SHOES_LEATHER_SET3": {"tipo": "shoes", "habilidades": [
        {"slot": "F", "nome": "Esquiva"},
        {"slot": "P1", "nome": "Mensageiro"},
    ]},
    "SHOES_CLOTH_SET1": {"tipo": "shoes", "habilidades": [
        {"slot": "F", "nome": "Esquiva"},
        {"slot": "P1", "nome": "Mensageiro"},
    ]},
    # ---- capa/bolsa/consumiveis (iguais pra quase todo mundo) ----
    "CAPEITEM_FW_FORTSTERLING": {"tipo": "cape", "habilidades": [
        {"slot": "P1", "nome": "Intocável"},
    ]},
    "BAG": {"tipo": "bag", "habilidades": [
        {"slot": "P1", "nome": "Estirão"},
    ]},
}


def _item_family(item_id):
    """'T8_2H_HAMMER@2' -> '2H_HAMMER'. Tira o tier (T\\d+_) e o encantamento
    (@\\d+) do item_id, sobrando so a familia que define as habilidades."""
    if not item_id:
        return None
    fam = re.sub(r"^T\d+_", "", item_id)
    fam = re.sub(r"@\d+$", "", fam)
    return fam


def get_item_abilities(item_id):
    """Devolve {"tipo": "mainhand"/"head"/..., "habilidades": [{"slot","nome"},...]}
    pro item_id dado, ou None se a familia dele ainda nao esta catalogada em
    ITEM_ABILITIES. Nao faz chamada de rede nenhuma - e so lookup local."""
    fam = _item_family(item_id)
    if not fam:
        return None
    return ITEM_ABILITIES.get(fam)


# =========================
# BIBLIOTECA DE BUILDS (loadouts salvos, reaproveitaveis em qualquer slot/comp)
# =========================

def load_build_library(guild_id=None):
    data = _load_json(_guild_file(guild_id, "builds_library.json"), {"builds": []})
    return data.get("builds", [])


def save_build_library(builds, guild_id=None):
    _save_json(_guild_file(guild_id, "builds_library.json"), {"builds": builds})


# =========================
# BUILDS PUBLICAS (compartilhadas pelos players/guildas na pagina /builds do
# site - agrega builds_library.json de TODOS os workspaces que tiverem pelo
# menos uma build marcada visibility="shared". Ratings ficam num arquivo
# site-wide separado (nao e por-guilda, e uma nota publica de qualquer
# jogador logado, tipo albiononlinebuilds.com).
# =========================

RATINGS_FILE = BASE_DIR / "ratings.json"


def list_all_workspace_ids():
    """Todos os ids de pasta em guild_data/ - guildas (numeros) e espacos
    pessoais (user_*). Usado pelo agregador publico de builds compartilhadas
    (percorre todo mundo, nao so a guilda do contexto atual)."""
    if not GUILD_DATA_DIR.exists():
        return []
    return [d.name for d in GUILD_DATA_DIR.iterdir() if d.is_dir()]


def load_ratings():
    return _load_json(RATINGS_FILE, {})


def save_ratings(data):
    _save_json(RATINGS_FILE, data)


# =========================
# ROSTER DE CLASSES (roster simples, usado por /ping-funcao e como fallback de evento)
# =========================

DEFAULT_CLASSES = [
    {"id": "healer_sagrado", "nome": "Healer Sagrado", "item_id": "T4_2H_HOLYSTAFF", "arma_nome": None, "cargo": "Healer Sagrado", "vagas": None, "ordem": 1, "role_id": None, "emoji_id": None},
    {"id": "healer_natureza", "nome": "Healer Natureza", "item_id": "T4_2H_NATURESTAFF", "arma_nome": None, "cargo": "Healer Natureza", "vagas": None, "ordem": 2, "role_id": None, "emoji_id": None},
    {"id": "frost_dps", "nome": "Frost DPS", "item_id": "T4_2H_FROSTSTAFF", "arma_nome": None, "cargo": "Frost DPS", "vagas": None, "ordem": 3, "role_id": None, "emoji_id": None},
    {"id": "fire_dps", "nome": "Fire DPS", "item_id": "T4_2H_FIRESTAFF", "arma_nome": None, "cargo": "Fire DPS", "vagas": None, "ordem": 4, "role_id": None, "emoji_id": None},
    {"id": "arcano_support", "nome": "Arcano Support", "item_id": "T4_2H_ARCANESTAFF", "arma_nome": None, "cargo": "Arcano Support", "vagas": None, "ordem": 5, "role_id": None, "emoji_id": None},
    {"id": "curse_dps", "nome": "Curse DPS", "item_id": "T4_2H_CURSEDSTAFF", "arma_nome": None, "cargo": "Curse DPS", "vagas": None, "ordem": 6, "role_id": None, "emoji_id": None},
    {"id": "tank_alabarda", "nome": "Tank Alabarda", "item_id": "T4_2H_HALBERD", "arma_nome": None, "cargo": "Tank Alabarda", "vagas": None, "ordem": 7, "role_id": None, "emoji_id": None},
    {"id": "tank_martelo", "nome": "Tank Martelo", "item_id": "T4_2H_HAMMER", "arma_nome": None, "cargo": "Tank Martelo", "vagas": None, "ordem": 8, "role_id": None, "emoji_id": None},
    {"id": "dps_machado", "nome": "DPS Machado", "item_id": "T4_2H_AXE", "arma_nome": None, "cargo": "DPS Machado", "vagas": None, "ordem": 9, "role_id": None, "emoji_id": None},
    {"id": "gank_adagas", "nome": "Gank Adagas", "item_id": "T4_2H_DAGGERPAIR", "arma_nome": None, "cargo": "Gank Adagas", "vagas": None, "ordem": 10, "role_id": None, "emoji_id": None},
    {"id": "dps_besta", "nome": "DPS Besta", "item_id": "T4_2H_CROSSBOWLARGE", "arma_nome": None, "cargo": "DPS Besta", "vagas": None, "ordem": 11, "role_id": None, "emoji_id": None},
    {"id": "dps_arco", "nome": "DPS Arco", "item_id": "T4_2H_LONGBOW", "arma_nome": None, "cargo": "DPS Arco", "vagas": None, "ordem": 12, "role_id": None, "emoji_id": None},
]


def load_classes(guild_id=None):
    data = _load_json(_guild_file(guild_id, "classes.json"), {"classes": DEFAULT_CLASSES})
    classes = data.get("classes", [])
    classes.sort(key=lambda c: c.get("ordem", 999))
    return classes


def save_classes(classes, guild_id=None):
    _save_json(_guild_file(guild_id, "classes.json"), {"classes": classes})


def _slots_from_legacy_classes(classes):
    """Roster simples (classes.json) tratado como slots de 1 build so - usado
    quando um evento e criado sem escolher composicao."""
    slots = []
    for c in classes:
        slots.append({
            "id": c["id"], "nome": c["nome"], "cargo": c["cargo"], "vagas": c.get("vagas"),
            "ordem": c.get("ordem", 1), "role_id": c.get("role_id"),
            "item_id": c.get("item_id"), "emoji_id": c.get("emoji_id"),
            "builds": [{"nome": c["nome"], "item_id": c.get("item_id"), "arma_nome": c.get("arma_nome"), "emoji_id": c.get("emoji_id")}],
        })
    return slots


# =========================
# CONTEUDOS (categorias) + COMPOSICOES (templates de evento com slots/builds)
# =========================

DEFAULT_CONTENT_TYPES = [
    {"id": "avalon_roads", "nome": "Avalon Roads", "ordem": 1},
    {"id": "gank", "nome": "Gank", "ordem": 2},
    {"id": "dg_de_grupo", "nome": "DG de Grupo", "ordem": 3},
    {"id": "apagas", "nome": "Apagas", "ordem": 4},
    {"id": "buff_avalon", "nome": "Buff Avalon", "ordem": 5},
    {"id": "dg_avalon", "nome": "DG Avalon", "ordem": 6},
    {"id": "fixa", "nome": "Fixa", "ordem": 7},
]

DEFAULT_TEMPLATES = [
    {
        "id": "zvz_padrao",
        "nome": "ZvZ Padrao",
        "conteudo_id": None,
        "slots": [
            {"id": "main_tank", "nome": "Main Tank", "cargo": "Main Tank", "vagas": 1, "ordem": 1, "role_id": None, "item_id": "T4_2H_HALBERD", "emoji_id": None,
             "builds": [{"nome": "Alabarda", "item_id": "T4_2H_HALBERD", "arma_nome": None, "emoji_id": None}, {"nome": "Martelo", "item_id": "T4_2H_HAMMER", "arma_nome": None, "emoji_id": None}]},
            {"id": "off_tank", "nome": "Off Tank", "cargo": "Off Tank", "vagas": 1, "ordem": 2, "role_id": None, "item_id": "T4_2H_HALBERD", "emoji_id": None,
             "builds": [{"nome": "Alabarda", "item_id": "T4_2H_HALBERD", "arma_nome": None, "emoji_id": None}, {"nome": "Martelo", "item_id": "T4_2H_HAMMER", "arma_nome": None, "emoji_id": None}]},
            {"id": "healer_sagrado", "nome": "Healer Sagrado", "cargo": "Healer Sagrado", "vagas": 2, "ordem": 3, "role_id": None, "item_id": "T4_2H_HOLYSTAFF", "emoji_id": None,
             "builds": [{"nome": "Cajado Sagrado", "item_id": "T4_2H_HOLYSTAFF", "arma_nome": None, "emoji_id": None}]},
            {"id": "healer_natureza", "nome": "Healer Natureza", "cargo": "Healer Natureza", "vagas": 1, "ordem": 4, "role_id": None, "item_id": "T4_2H_NATURESTAFF", "emoji_id": None,
             "builds": [{"nome": "Cajado da Natureza", "item_id": "T4_2H_NATURESTAFF", "arma_nome": None, "emoji_id": None}]},
            {"id": "frost_dps", "nome": "Frost DPS", "cargo": "Frost DPS", "vagas": 2, "ordem": 5, "role_id": None, "item_id": "T4_2H_FROSTSTAFF", "emoji_id": None,
             "builds": [{"nome": "Cajado de Gelo", "item_id": "T4_2H_FROSTSTAFF", "arma_nome": None, "emoji_id": None}]},
            {"id": "curse_dps", "nome": "Curse DPS", "cargo": "Curse DPS", "vagas": 2, "ordem": 6, "role_id": None, "item_id": "T4_2H_CURSEDSTAFF", "emoji_id": None,
             "builds": [{"nome": "Cajado Amaldicoado", "item_id": "T4_2H_CURSEDSTAFF", "arma_nome": None, "emoji_id": None}, {"nome": "Cajado Arcano", "item_id": "T4_2H_ARCANESTAFF", "arma_nome": None, "emoji_id": None}]},
            {"id": "dps_corpo", "nome": "DPS Corpo a Corpo", "cargo": "DPS Corpo a Corpo", "vagas": 3, "ordem": 7, "role_id": None, "item_id": "T4_2H_AXE", "emoji_id": None,
             "builds": [{"nome": "Machado Grande", "item_id": "T4_2H_AXE", "arma_nome": None, "emoji_id": None}, {"nome": "Adagas", "item_id": "T4_2H_DAGGERPAIR", "arma_nome": None, "emoji_id": None}]},
            {"id": "dps_ranged", "nome": "Ranged DPS", "cargo": "Ranged DPS", "vagas": 2, "ordem": 8, "role_id": None, "item_id": "T4_2H_CROSSBOWLARGE", "emoji_id": None,
             "builds": [{"nome": "Besta Pesada", "item_id": "T4_2H_CROSSBOWLARGE", "arma_nome": None, "emoji_id": None}, {"nome": "Arco Longo", "item_id": "T4_2H_LONGBOW", "arma_nome": None, "emoji_id": None}]},
        ],
    }
]


def _load_templates_file(guild_id=None):
    return _load_json(_guild_file(guild_id, "templates.json"), {"templates": DEFAULT_TEMPLATES, "content_types": DEFAULT_CONTENT_TYPES})


def load_templates(guild_id=None):
    data = _load_templates_file(guild_id)
    templates = data.get("templates", [])
    for t in templates:
        t.setdefault("conteudo_id", None)
    return templates


def save_templates(templates, guild_id=None):
    data = _load_templates_file(guild_id)
    data["templates"] = templates
    _save_json(_guild_file(guild_id, "templates.json"), data)


def load_content_types(guild_id=None):
    data = _load_templates_file(guild_id)
    tipos = data.get("content_types", [])
    tipos.sort(key=lambda c: c.get("ordem", 999))
    return tipos


def save_content_types(content_types, guild_id=None):
    data = _load_templates_file(guild_id)
    data["content_types"] = content_types
    _save_json(_guild_file(guild_id, "templates.json"), data)


# =========================
# EVENTOS ATIVOS / HISTORICO
# =========================

def load_events(guild_id=None):
    return _load_json(_guild_file(guild_id, "active_events.json"), {})


def save_events(events, guild_id=None):
    _save_json(_guild_file(guild_id, "active_events.json"), events)


def load_history(guild_id=None):
    return _load_json(_guild_file(guild_id, "historico_eventos.json"), {})


def _archive_event(event_id, events, guild_id=None):
    history = load_history(guild_id)
    history[event_id] = events.pop(event_id)
    _save_json(_guild_file(guild_id, "historico_eventos.json"), history)
    save_events(events, guild_id)


# =========================
# PING DE CONTEUDO (topico + reacao por funcao + vagas por ordem de chegada)
# =========================

def load_pings(guild_id=None):
    return _load_json(_guild_file(guild_id, "active_pings.json"), {})


def save_pings(pings, guild_id=None):
    _save_json(_guild_file(guild_id, "active_pings.json"), pings)


def _strip_accents(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _parse_slots_texto(texto):
    """'Tank\\nTank\\nHealer pt\\nSuporte\\nDPS\\nDPS' -> [{"nome":"Tank","vagas":2}, ...].
    Cada repeticao do mesmo nome (uma por linha, ou separado por virgula se nao
    tiver quebra de linha nenhuma) soma +1 vaga pra aquela funcao, na ordem em
    que apareceu primeiro."""
    texto = (texto or "").strip()
    if not texto:
        return []
    partes = texto.split("\n") if "\n" in texto else texto.split(",")
    ordem = []
    vagas = {}
    for parte in partes:
        nome = parte.strip()
        if not nome:
            continue
        chave = nome.lower()
        if chave not in vagas:
            vagas[chave] = {"nome": nome, "vagas": 0}
            ordem.append(chave)
        vagas[chave]["vagas"] += 1
    return [vagas[k] for k in ordem]


_EMOJI_THEMES = [
    (("tank",), "\U0001F6E1️"),
    (("heal", "cura"), "\U0001F49A"),
    (("suporte", "support", "buff"), "\U0001F527"),
    (("dps", "dano", "damage", "corpo", "melee"), "⚔️"),
    (("range", "arco", "besta", "bow", "crossbow"), "\U0001F3F9"),
    (("cavalaria", "montaria", "mount", "cavalo"), "\U0001F40E"),
    (("scout", "batedor", "flank"), "\U0001F441️"),
    (("cursed", "amaldi", "curse"), "\U0001F52E"),
    (("fire", "fogo"), "\U0001F525"),
    (("frost", "gelo", "ice"), "❄️"),
]
_NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
                  "6️⃣", "7️⃣", "8️⃣", "9️⃣", "\U0001F51F"]


def _assign_emojis(funcoes):
    """Da um emoji unico pra cada funcao - tenta um tematico pelo nome (tank/heal/
    dps/...) e cai pra numero (1..10) se nao achar ou se ja tiver sido usado
    (nunca repete emoji no mesmo ping, senao a reacao fica ambigua)."""
    used = set()
    for f in funcoes:
        norm = _strip_accents(f["nome"]).lower()
        chosen = None
        for keywords, emoji in _EMOJI_THEMES:
            if emoji in used:
                continue
            if any(kw in norm for kw in keywords):
                chosen = emoji
                break
        if not chosen:
            chosen = next((e for e in _NUMBER_EMOJIS if e not in used), None)
        if not chosen:
            chosen = "❔"  # esgotou tema e numero (mais de 10+10 funcoes) - generico
        used.add(chosen)
        f["emoji"] = chosen
    return funcoes


# =========================
# DIVISAO DE LOOT
# =========================

def compute_split(participantes_tempo: dict, profit: float, has_buyer: bool, reparo: float = 0.0, taxa_pct: float = None):
    """Divide o profit proporcionalmente ao tempo de presenca de cada um.
    Formula confirmada: pct_i = tempo_i / soma(tempo de todos) -> sempre soma 100%.
    taxa_pct: percentual da taxa de venda cobrada pelo comprador (ex: 15 = 15%),
    informado por quem fecha a divisao. Se nao informado, cai no BUYER_FEE padrao
    (compatibilidade com chamadas antigas)."""
    total = sum(participantes_tempo.values())
    if total <= 0:
        raise ValueError("Sem tempo registrado.")
    taxa = (taxa_pct / 100) if taxa_pct is not None else BUYER_FEE
    liquido = (profit - reparo) * (1 - taxa) if has_buyer else profit
    shares = {uid: {"pct": t / total, "valor": liquido * (t / total)} for uid, t in participantes_tempo.items()}
    return liquido, shares


# =========================
# REPUTACAO (avaliacoes pos-ping/evento: caller e players)
# =========================
# Cada avaliacao (rating) tem o formato:
#   {"id", "ping_key", "rater_id", "rated_id", "papel_avaliado" ("caller"|"player"),
#    "positivo" (bool), "comentario" (str|""), "timestamp" (ISO str)}
# Guardadas em REPUTATION_FILE como {"avaliacoes": [...]}.

def load_reputation(guild_id=None):
    return _load_json(_guild_file(guild_id, "reputacao.json"), {"avaliacoes": []})


def save_reputation(data, guild_id=None):
    _save_json(_guild_file(guild_id, "reputacao.json"), data)


def add_rating(ping_key, rater_id, rated_id, papel_avaliado, positivo, comentario="",
                rater_name=None, rated_name=None, guild_id=None):
    """Registra uma avaliacao. Evita duplicata: se o mesmo avaliador ja avaliou
    a mesma pessoa no mesmo ping/evento, substitui a avaliacao anterior em vez
    de duplicar (deixa o avaliador corrigir o voto se clicar de novo).

    rater_name/rated_name sao opcionais (best-effort) - guardados so pra o site
    conseguir mostrar nome em vez de ID cru sem precisar bater na API do Discord."""
    data = load_reputation(guild_id)
    avaliacoes = data.setdefault("avaliacoes", [])

    avaliacoes[:] = [
        a for a in avaliacoes
        if not (a.get("ping_key") == ping_key
                and str(a.get("rater_id")) == str(rater_id)
                and str(a.get("rated_id")) == str(rated_id))
    ]

    rating = {
        "id": uuid.uuid4().hex[:12],
        "ping_key": ping_key,
        "rater_id": str(rater_id),
        "rated_id": str(rated_id),
        "rater_name": rater_name,
        "rated_name": rated_name,
        "papel_avaliado": papel_avaliado,
        "positivo": bool(positivo),
        "comentario": (comentario or "").strip(),
        "timestamp": datetime.now(TIMEZONE).isoformat(),
    }
    avaliacoes.append(rating)
    save_reputation(data, guild_id)
    return rating


def _badges_for_stats(stats):
    """Calcula badges de conquista a partir das stats agregadas de um usuario.
    So concede badge quando ha volume minimo de avaliacoes (evita badge com 1 voto)."""
    badges = []
    total = stats["total"]
    pct = stats["pct_positivo"]
    caller_total = stats["como_caller"]["total"]
    caller_pct = stats["como_caller"]["pct_positivo"]

    if total >= 20 and stats["negativos"] == 0:
        badges.append("💯 Nota máxima")
    elif total >= 10 and pct >= 90:
        badges.append("🌟 Bem avaliado")

    if caller_total >= 5 and caller_pct >= 85:
        badges.append("🎖️ Caller confiável")

    if total >= 30:
        badges.append("🏆 Veterano")

    return badges


def compute_reputation_stats(user_id, papel=None, guild_id=None):
    """Agrega as avaliacoes recebidas por user_id. Se `papel` for passado
    ("caller" ou "player"), filtra so aquele papel; senao considera tudo.
    Retorna totais, % positivo, ultimos comentarios e badges."""
    data = load_reputation(guild_id)
    todas = [a for a in data.get("avaliacoes", []) if str(a.get("rated_id")) == str(user_id)]

    def _resumo(avaliacoes):
        total = len(avaliacoes)
        positivos = sum(1 for a in avaliacoes if a.get("positivo"))
        negativos = total - positivos
        pct = round((positivos / total) * 100, 1) if total else 0.0
        return {"total": total, "positivos": positivos, "negativos": negativos, "pct_positivo": pct}

    geral = _resumo(todas)
    geral["como_caller"] = _resumo([a for a in todas if a.get("papel_avaliado") == "caller"])
    geral["como_player"] = _resumo([a for a in todas if a.get("papel_avaliado") == "player"])

    com_comentario = [a for a in todas if a.get("comentario")]
    com_comentario.sort(key=lambda a: a.get("timestamp", ""), reverse=True)
    geral["ultimos_positivos"] = [a for a in com_comentario if a.get("positivo")][:3]
    geral["ultimos_negativos"] = [a for a in com_comentario if not a.get("positivo")][:3]

    geral["badges"] = _badges_for_stats(geral)

    # melhor nome de exibicao disponivel: pega o mais recente que alguem usou
    # pra avaliar essa pessoa (best-effort, pode ser None se nunca foi avaliado)
    nomes = [a.get("rated_name") for a in sorted(todas, key=lambda a: a.get("timestamp", ""), reverse=True) if a.get("rated_name")]
    geral["nome"] = nomes[0] if nomes else None

    if papel:
        return geral[f"como_{papel}"] | {"badges": geral["badges"],
                                          "ultimos_positivos": geral["ultimos_positivos"],
                                          "ultimos_negativos": geral["ultimos_negativos"],
                                          "nome": geral["nome"]}
    return geral


def reputation_leaderboard(limit=10, min_ratings=1, guild_id=None):
    """Ranking geral: agrega todas as avaliacoes por pessoa avaliada, ordena por
    % positivo (com volume minimo pra nao deixar 1 voto isolado no topo) e depois
    por total de avaliacoes. Usado no dashboard do site (sem precisar de login)."""
    data = load_reputation(guild_id)
    por_pessoa = {}
    for a in data.get("avaliacoes", []):
        uid = str(a.get("rated_id"))
        por_pessoa.setdefault(uid, []).append(a)

    ranking = []
    for uid, avaliacoes in por_pessoa.items():
        total = len(avaliacoes)
        if total < min_ratings:
            continue
        positivos = sum(1 for a in avaliacoes if a.get("positivo"))
        pct = round((positivos / total) * 100, 1) if total else 0.0
        nomes = [a.get("rated_name") for a in sorted(avaliacoes, key=lambda a: a.get("timestamp", ""), reverse=True) if a.get("rated_name")]
        ranking.append({
            "user_id": uid,
            "nome": nomes[0] if nomes else None,
            "total": total,
            "positivos": positivos,
            "negativos": total - positivos,
            "pct_positivo": pct,
        })

    ranking.sort(key=lambda r: (r["pct_positivo"], r["total"]), reverse=True)
    return ranking[:limit]


# =========================
# SALDO (loot creditado, em vez de pago na hora)
# =========================
# Cada guilda tem um arquivo saldos.json: {"saldos": {discord_id: {"nome","valor"}},
# "historico": [{"discord_id","nome","valor","tipo","motivo","timestamp","por_discord_id","por_nome"}]}
# "tipo" e "credito" (loot saldado) ou "quitacao" (baixa manual do gestor).
#
# Gestores de saldo (quem pode consultar saldo de terceiros e dar baixa):
# - o dono do slot de guilda (quem contratou/paga o bot pra essa guilda);
# - qualquer discord_id extra cadastrado em saldo_gestores.json pelo dono do slot.
# Isso e INDEPENDENTE de cargo/permissao do Discord (Administrador etc.) - e
# controlado pelo contratante, dentro do bot ou pelo dashboard.

def load_saldos(guild_id=None):
    return _load_json(_guild_file(guild_id, "saldos.json"), {"saldos": {}, "historico": []})


def save_saldos(data, guild_id=None):
    _save_json(_guild_file(guild_id, "saldos.json"), data)


def load_saldo_gestores(guild_id=None):
    return _load_json(_guild_file(guild_id, "saldo_gestores.json"), {"gestores": []}).get("gestores", [])


def save_saldo_gestores(gestores, guild_id=None):
    _save_json(_guild_file(guild_id, "saldo_gestores.json"), {"gestores": gestores})


def owner_discord_id_for_guild(guild_id):
    """Discord ID de quem contratou/paga o slot de guilda ativo dessa guilda
    (dono do slot). Retorna None se essa guilda nao tiver slot ativo (nao
    deveria acontecer em uso normal, mas o bot nao deve quebrar por isso)."""
    guild_id = str(guild_id)
    for s in load_slots():
        if s.get("type") == "guild" and str(s.get("guild_id")) == guild_id and s.get("status", "active") == "active":
            return str(s.get("owner_discord_id"))
    return None


def is_saldo_gestor(guild_id, discord_id):
    """True se esse discord_id pode consultar saldo de terceiros e dar baixa
    (quitar) nessa guilda: e o dono do slot, ou foi adicionado como gestor
    extra pelo dono do slot."""
    discord_id = str(discord_id)
    if discord_id == str(owner_discord_id_for_guild(guild_id)):
        return True
    return discord_id in [str(g) for g in load_saldo_gestores(guild_id)]


def add_saldo(guild_id, discord_id, nome, valor, motivo="Loot saldado", por_discord_id=None, por_nome=None):
    """Credita `valor` (pode ser negativo, mas normalmente positivo) no saldo
    acumulado de discord_id nessa guilda. Registra no historico."""
    discord_id = str(discord_id)
    data = load_saldos(guild_id)
    saldos = data.setdefault("saldos", {})
    atual = saldos.get(discord_id, {"nome": nome, "valor": 0.0})
    atual["nome"] = nome or atual.get("nome")
    atual["valor"] = float(atual.get("valor", 0.0)) + float(valor)
    saldos[discord_id] = atual
    data.setdefault("historico", []).append({
        "discord_id": discord_id,
        "nome": nome,
        "valor": float(valor),
        "tipo": "credito",
        "motivo": motivo,
        "timestamp": _now_iso(),
        "por_discord_id": str(por_discord_id) if por_discord_id else None,
        "por_nome": por_nome,
    })
    save_saldos(data, guild_id)
    return atual["valor"]


# =========================
# PERMISSOES DA EQUIPE (por guilda)
# =========================
# Tres niveis, do maior pro menor:
#   admin  - administrador do servidor (Administrador ou Gerenciar Servidor no
#            Discord) ou o contratante do slot. Faz tudo e decide quem e o que.
#   gestor - mexe no saldo (ver, adicionar, remover, quitar) e tambem pode pingar.
#   caller - pode pingar conteudo, criar/encerrar eventos e montar composicoes.
#            NAO mexe no saldo.
# Cada nivel aceita pessoas (discord_id) e cargos do Discord (role_id).
# Os gestores-pessoa continuam em saldo_gestores.json (compat com o que ja
# existia); o resto fica em permissoes.json.

NIVEIS_PERMISSAO = ("caller", "gestor")


def load_permissoes(guild_id=None):
    data = _load_json(_guild_file(guild_id, "permissoes.json"), {})
    out = {}
    for nivel in NIVEIS_PERMISSAO:
        bloco = data.get(nivel) or {}
        out[nivel] = {
            "pessoas": [str(x) for x in bloco.get("pessoas", [])],
            "cargos": [str(x) for x in bloco.get("cargos", [])],
        }
    # gestores-pessoa sempre vem do arquivo antigo (fonte unica)
    out["gestor"]["pessoas"] = [str(x) for x in load_saldo_gestores(guild_id)]
    return out


def save_permissoes(perms, guild_id=None):
    save_saldo_gestores(perms["gestor"]["pessoas"], guild_id)
    data = {n: {"pessoas": perms[n]["pessoas"] if n != "gestor" else [], "cargos": perms[n]["cargos"]} for n in NIVEIS_PERMISSAO}
    _save_json(_guild_file(guild_id, "permissoes.json"), data)


def set_permissao(guild_id, nivel, tipo, alvo_id, adicionar=True):
    """tipo = 'pessoas' ou 'cargos'. Devolve True se mudou algo."""
    if nivel not in NIVEIS_PERMISSAO or tipo not in ("pessoas", "cargos"):
        raise ValueError("nivel/tipo invalido")
    perms = load_permissoes(guild_id)
    lista = perms[nivel][tipo]
    alvo_id = str(alvo_id)
    if adicionar and alvo_id not in lista:
        lista.append(alvo_id)
    elif not adicionar and alvo_id in lista:
        lista.remove(alvo_id)
    else:
        return False
    save_permissoes(perms, guild_id)
    return True


def nivel_permissao(guild_id, discord_id, role_ids=(), eh_admin_discord=False):
    """'admin', 'gestor', 'caller' ou None. role_ids = cargos que a pessoa
    tem no servidor; eh_admin_discord = tem Administrador/Gerenciar Servidor."""
    discord_id = str(discord_id)
    if eh_admin_discord or discord_id == str(owner_discord_id_for_guild(guild_id)):
        return "admin"
    perms = load_permissoes(guild_id)
    roles = {str(r) for r in role_ids}
    for nivel in ("gestor", "caller"):
        if discord_id in perms[nivel]["pessoas"] or roles & set(perms[nivel]["cargos"]):
            return nivel
    return None


def ajustar_saldo(guild_id, discord_id, nome, valor, motivo, por_discord_id, por_nome=None):
    """Ajuste manual do gestor: valor positivo adiciona, negativo remove."""
    valor = float(valor)
    if valor >= 0:
        return add_saldo(guild_id, discord_id, nome, valor, motivo=motivo or "Ajuste manual",
                         por_discord_id=por_discord_id, por_nome=por_nome)
    novo = quitar_saldo(guild_id, discord_id, por_discord_id, por_nome=por_nome, valor=-valor,
                        motivo=motivo or "Ajuste manual")
    if nome:
        data = load_saldos(guild_id)
        entrada = data.get("saldos", {}).get(str(discord_id))
        if entrada is not None and not entrada.get("nome"):
            entrada["nome"] = nome
            save_saldos(data, guild_id)
    return novo


def quitar_saldo(guild_id, discord_id, por_discord_id, por_nome=None, valor=None, motivo="Quitado pelo gestor"):
    """Zera (valor=None) ou desconta um valor especifico do saldo de
    discord_id. Retorna o novo saldo. Registra no historico quem deu a
    baixa (por_discord_id)."""
    discord_id = str(discord_id)
    data = load_saldos(guild_id)
    saldos = data.setdefault("saldos", {})
    atual = saldos.get(discord_id, {"nome": None, "valor": 0.0})
    valor_anterior = float(atual.get("valor", 0.0))
    if valor is None:
        descontado = valor_anterior
        atual["valor"] = 0.0
    else:
        descontado = float(valor)
        atual["valor"] = valor_anterior - descontado
    saldos[discord_id] = atual
    data.setdefault("historico", []).append({
        "discord_id": discord_id,
        "nome": atual.get("nome"),
        "valor": -descontado,
        "tipo": "quitacao",
        "motivo": motivo,
        "timestamp": _now_iso(),
        "por_discord_id": str(por_discord_id),
        "por_nome": por_nome,
    })
    save_saldos(data, guild_id)
    return atual["valor"]


def _now_iso():
    import datetime
    return datetime.datetime.utcnow().isoformat() + "Z"
