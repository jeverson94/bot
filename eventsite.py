"""
Site de configuracao do bot de eventos STOMP - Conteudos, Composicoes e Builds.
=================================================================================
Le/escreve os MESMOS arquivos JSON que o bot do Discord usa (via albion_shared.py),
entao qualquer alteracao feita aqui aparece no bot na hora (e vice-versa) - nao tem
sincronizacao nenhuma pra fazer, os dois processos so compartilham a pasta.

Requisitos:
    pip install flask waitress requests

Rodar (dev):
    python3 eventsite.py
Rodar (producao, usado no systemd):
    python3 -m waitress --host=0.0.0.0 --port=8000 eventsite:app

Sem senha por enquanto (decisao do Jeverson) - qualquer um com o link edita.
Pra adicionar senha depois: e so colocar um @app.before_request checando um
cookie/header, ja deixei o layout preparado pra isso (ver funcao `_check_auth`).
"""

import copy
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import quote

import requests
from flask import Flask, abort, jsonify, redirect, render_template_string, request, url_for

import albion_shared as shared

app = Flask(__name__)

INTERNAL_API_BASE = f"http://127.0.0.1:{os.environ.get('INTERNAL_API_PORT', '8100')}"
INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET", "")


def _internal_call(method, path, guild_id=None, **json_extra):
    """Chama a API interna do bot (mesma VM, so localhost) pra acoes que
    precisam da conexao viva com o Discord: listar canais, criar/editar/
    encerrar ping e postar a divisao de loot. Devolve (dict_resposta, status)."""
    payload = dict(json_extra)
    if guild_id is not None:
        payload["guild_id"] = int(guild_id)
    headers = {"X-Internal-Secret": INTERNAL_API_SECRET} if INTERNAL_API_SECRET else {}
    try:
        if method == "GET":
            resp = requests.get(f"{INTERNAL_API_BASE}{path}", params=payload, headers=headers, timeout=15)
        else:
            resp = requests.request(method, f"{INTERNAL_API_BASE}{path}", json=payload, headers=headers, timeout=30)
    except requests.RequestException:
        return {"error": "Bot fora do ar - tenta de novo em alguns segundos."}, 503
    try:
        data = resp.json()
    except ValueError:
        data = {"error": "resposta invalida do bot"}
    return data, resp.status_code

SEM_CATEGORIA = "_sem"

BUILD_CATEGORIES = [
    "Mists Solo", "Hellgate 2v2", "Hellgate 5v5", "Masmorras Corrompidas",
    "Gank (Grupo)", "Depths 3v3", "Solo Bomb", "Bomb (Grupo)",
    "Mundo Aberto (Solo)", "Roaming Avalon", "PVE em Grupo", "Pequeno Combate (ZvZ)",
]

# Funcao/role da build na party - usado so pra mostrar uma bolinha colorida
# nos cards da pagina publica de builds (visual tipo albionzvz.com), nao
# afeta nada do sistema de composicoes/pings que ja existe.
BUILD_ROLES = {
    "caller": {"label": "Caller", "cor": "#e2bc62"},
    "maintank": {"label": "Main Tank", "cor": "#5aa7e6"},
    "offtank": {"label": "Off Tank", "cor": "#7ec8e3"},
    "suporte": {"label": "Suporte", "cor": "#9b7ee8"},
    "cura": {"label": "Cura", "cor": "#6fd48a"},
    "dps": {"label": "Dano (DPS)", "cor": "#e66a6a"},
    "montaria": {"label": "Montaria de Combate", "cor": "#e69a4a"},
}


def _check_auth():
    """Mantido por compat (nao faz mais nada sozinho - a autenticacao real
    agora e o _gate() abaixo, com sessao Discord OAuth por guilda)."""
    return True


_PUBLIC_PATHS = {"/api/health", "/auth/discord/login", "/auth/discord/callback", "/auth/logout", "/legacy/enter", "/webhooks/stripe", "/api/public/builds"}
_SESSION_ONLY_PATHS = {"/api/me", "/api/slots/personal", "/api/slots/guild"}  # exige login, mas nao e sobre uma guilda especifica


@app.before_request
def _gate():
    """Autenticacao + isolamento por guilda de TODA rota, num lugar so:
      - publicas (login/callback/health/logout/ponte do painel legado): direto.
      - /api/me: so exige sessao (identidade do usuario, nao dado de guilda).
      - /api/* (dashboard novo, chamado via fetch com Bearer token): exige
        sessao + guild_id valido (?guild_id= na query, ou a unica guilda do
        usuario se so administrar uma) - ver require_guild_session().
      - qualquer outra rota (painel avancado/legado, HTML navegado direto no
        browser): exige o cookie criado por /legacy/enter.
    Nos dois ultimos casos a guilda resolvida fica no contexto ambiente
    (shared.set_current_guild) - o resto da rota chama load_/save_* normal,
    sem precisar passar guild_id em cada chamada (mesmo mecanismo do bot)."""
    if request.method == "OPTIONS":
        # preflight de CORS - o navegador manda ISSO sem o header Authorization
        # de proposito (e so uma pergunta "posso mandar esse header depois?"),
        # entao nunca teria sessao valida aqui. Deixa passar sem checar nada -
        # o after_request (_cors) decora a resposta com os headers de permissao.
        return None
    if not _check_auth():
        abort(403)
    path = request.path
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return None
    if path in _SESSION_ONLY_PATHS or (path.startswith("/api/public/builds/") and path.endswith("/rate")):
        require_session()
        return None
    if path.startswith("/api/"):
        require_guild_session()
        return None
    ctx = _legacy_context()
    if not ctx:
        return redirect(f"{FRONTEND_URL}/dashboard/config?erro=painel_legado_expirado")
    _, guild_id = ctx
    shared.set_current_guild(guild_id)
    return None


@app.teardown_request
def _reset_guild_context(exception=None):
    """Fecha o contexto ambiente no fim de TODA requisicao (mesmo com erro) -
    evita que, com o Waitress reaproveitando threads entre requests, o
    contexto de uma guilda "vaze" pra proxima requisicao que cair na mesma
    thread (se algo falhar, cai no DEFAULT_GUILD_ID de novo, nunca numa
    guilda errada)."""
    shared.set_current_guild(None)


@app.after_request
def _cors(response):
    # API publica de leitura (sem dado sensivel) consumida pelo site novo
    # (dominio diferente, hospedado fora daqui) - libera geral.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


# =========================================================================
# VISUAL - shell compartilhado (dark + dourado, no clima do bot/Albion)
# =========================================================================

BASE_CSS = """
* { box-sizing: border-box; }
:root {
  --bg: #0b0e14;
  --bg-soft: #151924;
  --card: #12151f;
  --card-border: rgba(226,188,98,.10);
  --gold: #e2bc62;
  --gold-soft: rgba(226,188,98,.14);
  --text: #dfe3ea;
  --text-dim: #8b93a3;
  --danger: #e5686a;
  --danger-soft: rgba(229,104,106,.12);
  --ok: #57b98a;
  --ok-soft: rgba(87,185,138,.12);
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 12px 32px -12px rgba(0,0,0,.55);
}
html, body { margin:0; padding:0; background: radial-gradient(1200px 700px at 20% -10%, #161b29 0%, var(--bg) 55%); color: var(--text); font-family: 'Inter', system-ui, -apple-system, sans-serif; -webkit-font-smoothing: antialiased; }
h1, h2, h3, .brand { font-family: 'Cinzel', serif; }
a { color: var(--gold); text-decoration: none; }
a:hover { text-decoration: underline; }
.topbar { display:flex; align-items:center; justify-content:space-between; padding: 16px 32px; border-bottom: 1px solid var(--card-border); position: sticky; top:0; background: rgba(11,14,20,.85); backdrop-filter: blur(10px); z-index: 10; }
.brand { font-size: 19px; letter-spacing: 2.5px; color: var(--gold); }
.brand span { color: var(--text-dim); font-family: 'Inter', sans-serif; font-size: 12.5px; letter-spacing: 0; }
.topbar nav a { margin-left: 26px; color: var(--text-dim); font-size: 13px; text-transform: uppercase; letter-spacing: 1.2px; font-weight: 500; transition: color .15s ease; }
.topbar nav a:hover { color: var(--gold); text-decoration: none; }
main { max-width: 1100px; margin: 0 auto; padding: 40px 24px 90px; }
.crumb { color: var(--text-dim); font-size: 13px; margin-bottom: 20px; }
.crumb a { color: var(--text-dim); }
.crumb a:hover { color: var(--gold); }
h1 { font-size: 28px; margin: 0 0 6px; color: #fff; font-weight: 600; letter-spacing: .3px; }
h2 { font-size: 18px; color: var(--gold); margin: 40px 0 16px; border-bottom: 1px solid var(--card-border); padding-bottom: 10px; font-weight: 600; letter-spacing: .3px; }
.subtitle { color: var(--text-dim); margin-bottom: 30px; font-size: 14px; line-height: 1.6; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 14px; }
.card { background: var(--card); border: 1px solid var(--card-border); border-radius: 12px; padding: 20px; transition: border-color .15s ease, transform .15s ease, box-shadow .15s ease; box-shadow: var(--shadow); }
.card.link { display:block; cursor:pointer; }
.card.link:hover { border-color: rgba(226,188,98,.4); transform: translateY(-2px); text-decoration:none; }
.card h3 { margin: 0 0 6px; font-size: 16px; color: #fff; font-weight: 600; }
.card .meta { color: var(--text-dim); font-size: 13px; line-height: 1.5; }
.pill { display:inline-block; background: var(--gold-soft); color: var(--gold); font-size: 11px; font-weight: 600; padding: 3px 10px; border-radius: 999px; margin-top: 10px; letter-spacing: .3px; }
.new-card { border-style: dashed; border-color: rgba(226,188,98,.25); box-shadow:none; display:flex; align-items:center; justify-content:center; color: var(--text-dim); font-size: 14px; min-height: 88px; }
.new-card:hover { color: var(--gold); border-color: var(--gold); transform:none; }
form.inline { display:flex; gap: 8px; margin-top: 6px; }
input[type=text], input[type=number], textarea, select {
  background: var(--bg-soft); border: 1px solid rgba(255,255,255,.08); color: var(--text);
  padding: 10px 12px; border-radius: 8px; font-size: 14px; font-family: inherit; width: 100%;
  transition: border-color .15s ease, box-shadow .15s ease;
}
input:focus, textarea:focus, select:focus { outline: none; border-color: var(--gold); box-shadow: 0 0 0 3px rgba(226,188,98,.15); }
input::placeholder, textarea::placeholder { color: #565d6c; }
textarea { min-height: 64px; resize: vertical; }
label { display:flex; align-items:center; font-size: 12px; color: var(--text-dim); margin-bottom: 6px; text-transform: uppercase; letter-spacing: .6px; font-weight: 600; }
.field { margin-bottom: 16px; }
.btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; background: var(--gold); color: #191305; font-weight: 600; border: none; padding: 10px 18px; border-radius: 8px; cursor: pointer; font-size: 14px; transition: filter .15s ease, transform .1s ease; }
.btn:hover { filter: brightness(1.1); }
.btn:active { transform: translateY(1px); }
.btn.secondary { background: transparent; border: 1px solid rgba(226,188,98,.35); color: var(--gold); }
.btn.secondary:hover { background: var(--gold-soft); filter:none; }
.btn.danger { background: transparent; border: 1px solid rgba(229,104,106,.4); color: var(--danger); }
.btn.danger:hover { background: var(--danger-soft); filter:none; }
.btn.small { padding: 6px 12px; font-size: 12.5px; }
.slot-card { background: var(--card); border: 1px solid var(--card-border); border-radius: 12px; padding: 20px 22px; margin-bottom: 14px; box-shadow: var(--shadow); }
.slot-head { display:flex; justify-content:space-between; align-items:flex-start; gap: 10px; flex-wrap: wrap; }
.slot-head h3 { margin: 0; font-size: 16px; color:#fff; font-weight:600; }
.slot-head .meta { color: var(--text-dim); font-size: 13px; margin-top:3px; }
.builds-row { display:flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
.build-chip { display:flex; align-items:center; gap: 8px; background: var(--bg-soft); border: 1px solid rgba(255,255,255,.08); padding: 6px 12px 6px 6px; border-radius: 999px; font-size: 13px; }
.build-chip img { width: 32px; height: 32px; border-radius: 6px; background: #0007; }
.build-chip .noicon { width:32px; height:32px; border-radius:6px; background:#2a2e40; display:flex; align-items:center; justify-content:center; font-size:10px; color:var(--text-dim); }
.box { background: var(--card); border: 1px solid var(--card-border); border-radius: 12px; padding: 24px; margin-top: 20px; box-shadow: var(--shadow); }
.box h3 { margin-top:0; color: var(--gold); font-size: 14px; text-transform: uppercase; letter-spacing: .6px; font-weight:700; display:flex; align-items:center; }
.box .hint { color: var(--text-dim); font-size: 13px; line-height:1.6; margin: -8px 0 18px; }
.row2 { display:grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.row3 { display:grid; grid-template-columns: 2fr 1fr 1fr; gap: 14px; }
.empty { color: var(--text-dim); font-style: italic; padding: 34px 0; text-align:center; font-size: 13.5px; }
footer { text-align:center; color: var(--text-dim); font-size: 12.5px; padding: 30px 20px 50px; border-top: 1px solid var(--card-border); margin-top: 50px; }
footer .warn { color: #e0a34d; display:block; margin-bottom:6px; }
.flex-between { display:flex; justify-content:space-between; align-items:center; gap: 12px; flex-wrap: wrap; }
.tag-select { display:flex; gap:8px; flex-wrap:wrap; margin-top: 8px; }
.build-chip.link { cursor:pointer; }
.build-chip.link:hover { border-color: var(--gold); }
.build-chip .editar { color: var(--text-dim); font-size: 11px; margin-left: 4px; }

/* Tooltip de ajuda (icone "i" ao lado dos labels) */
.info-tip { position:relative; display:inline-flex; align-items:center; justify-content:center; width:14px; height:14px; border-radius:50%; background:rgba(226,188,98,.16); color:var(--gold); font-size:9.5px; font-weight:700; font-family: 'Inter', sans-serif; text-transform:none; letter-spacing:0; cursor:help; margin-left:6px; flex-shrink:0; }
.info-tip:hover, .info-tip:focus { background: var(--gold); color:#191305; outline:none; }
.info-tip:hover::after, .info-tip:focus::after {
  content: attr(data-tip); position:absolute; left:0; bottom:135%; transform:none;
  background:#1a1e2b; border:1px solid rgba(226,188,98,.3); color: var(--text); font-size:12.5px; font-weight:400;
  text-transform:none; letter-spacing:normal; padding:10px 12px; border-radius:8px; width:240px;
  text-align:left; white-space:normal; box-shadow: 0 12px 28px rgba(0,0,0,.5); z-index:30; line-height:1.5;
}
.info-tip:hover::before, .info-tip:focus::before {
  content:''; position:absolute; left:5px; bottom:118%; border:6px solid transparent; border-top-color:#1a1e2b; z-index:30;
}

/* Banner de erro/sucesso (validacao de formulario) */
.banner { display:flex; align-items:center; gap:10px; padding: 12px 16px; border-radius: 10px; font-size: 13.5px; margin-bottom: 18px; }
.banner.error { background: var(--danger-soft); border: 1px solid rgba(229,104,106,.3); color: #f5a3a4; }
.banner.ok { background: var(--ok-soft); border: 1px solid rgba(87,185,138,.3); color: #8fdcb6; }

/* Picker de builds da biblioteca (chips clicaveis, multi-selecao) */
.section-label { display:flex; align-items:center; font-size:12px; font-weight:700; color: var(--text); text-transform:uppercase; letter-spacing:.6px; margin: 4px 0 10px; }
.section-label .count { margin-left:8px; color: var(--gold); font-weight:600; }
.or-divider { display:flex; align-items:center; gap:12px; color: var(--text-dim); font-size:11.5px; text-transform:uppercase; letter-spacing:.8px; margin: 20px 0 14px; }
.or-divider::before, .or-divider::after { content:''; flex:1; height:1px; background: var(--card-border); }
.lib-picker { display:flex; flex-wrap:wrap; gap:8px; max-height:220px; overflow-y:auto; padding:2px; }
.build-chip.lib-chip { cursor:pointer; border:1px solid rgba(255,255,255,.08); font-family:inherit; font-size:13px; color:var(--text); transition: all .12s ease; }
.build-chip.lib-chip:hover { border-color: rgba(226,188,98,.4); }
.build-chip.lib-chip.selected { border-color: var(--gold); background: var(--gold-soft); box-shadow: 0 0 0 1px var(--gold) inset; }
.build-chip.lib-chip .check { display:none; width:16px; height:16px; border-radius:50%; background:var(--gold); color:#191305; font-size:10px; align-items:center; justify-content:center; margin-left:2px; }
.build-chip.lib-chip.selected .check { display:flex; }
.lib-empty { color: var(--text-dim); font-size:13px; font-style:italic; padding: 10px 2px; }

.paperdoll { display:grid; grid-template-columns: repeat(3, 108px); gap: 20px 18px; justify-content:center; margin: 30px 0 40px; }
.equip-cell { display:flex; flex-direction:column; align-items:center; gap:6px; }
.equip-slot { position:relative; width:100px; height:100px; background: var(--bg-soft); border:2px solid rgba(226,188,98,.25); border-radius:12px; display:flex; align-items:center; justify-content:center; cursor:pointer; transition: all .15s ease; }
.equip-slot:hover, .equip-slot.active { border-color: var(--gold); box-shadow: 0 0 0 3px rgba(226,188,98,.15); }
.equip-slot.dragover { border-color: var(--ok); background: rgba(76,175,125,.12); }
.equip-slot-inner { width:100%; height:100%; display:flex; align-items:center; justify-content:center; pointer-events:none; }
.equip-slot-inner img { max-width:78px; max-height:78px; pointer-events:none; }
.equip-slot .plus { color: var(--text-dim); font-size:28px; pointer-events:none; }
.equip-slot-label { text-align:center; font-size:11px; color:var(--text-dim); text-transform:uppercase; letter-spacing:.4px; white-space:nowrap; }
.equip-clear { position:absolute; top:-9px; right:-9px; width:22px; height:22px; border-radius:50%; background:var(--danger); color:#fff; border:none; cursor:pointer; font-size:13px; line-height:1; display:none; }
.equip-slot:hover .equip-clear, .equip-slot:focus-within .equip-clear { display:block; }
.swap-row { display:flex; flex-wrap:wrap; gap:4px; justify-content:center; max-width:108px; min-height:26px; border-radius:6px; }
.swap-row.dragover { background: rgba(76,175,125,.15); outline: 1px dashed var(--ok); }
.swap-chip { position:relative; width:26px; height:26px; border-radius:6px; overflow:hidden; border:1px solid rgba(226,188,98,.35); background:#0007; flex-shrink:0; }
.swap-chip img { width:100%; height:100%; object-fit:contain; }
.swap-x { position:absolute; inset:0; width:100%; height:100%; background:rgba(0,0,0,.8); color:#fff; border:none; font-size:13px; line-height:1; opacity:0; cursor:pointer; }
.swap-chip:hover .swap-x, .swap-chip:focus-within .swap-x { opacity:1; }
.swap-add { width:26px; height:26px; border-radius:6px; border:1px dashed rgba(226,188,98,.45); background:transparent; color:var(--text-dim); cursor:pointer; font-size:14px; line-height:1; flex-shrink:0; }
.equip-slot-abilities { display:flex; flex-wrap:wrap; gap:3px; justify-content:center; max-width:116px; min-height:0; }
.hab-chip { font-size:9.5px; line-height:1.3; color: var(--gold); background: rgba(226,188,98,.1); border:1px solid rgba(226,188,98,.25); border-radius:5px; padding:1px 5px; white-space:nowrap; }
.swap-add:hover, .swap-add:focus-visible { border-color: var(--gold); color: var(--gold); }
#palette-box { display:none; }
.palette-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(160px,1fr)); gap:8px; max-height:380px; overflow-y:auto; margin-top:14px; }
.palette-item { display:flex; align-items:center; gap:9px; background:var(--bg-soft); border:1px solid rgba(226,188,98,.15); border-radius:8px; padding:7px 10px; cursor:grab; font-size:13px; }
.palette-item:hover { border-color: var(--gold); background: var(--gold-soft); }
.palette-item img { width:34px; height:34px; flex-shrink:0; }

.wp { margin-bottom: 4px; }
.wp-chips { display:flex; flex-wrap:wrap; gap:8px; margin: 8px 0; }
.wp-chip { display:flex; align-items:center; gap:6px; background:var(--bg-soft); border:1px solid rgba(226,188,98,.25); border-radius:999px; padding:4px 6px 4px 4px; font-size:12.5px; }
.wp-chip img { width:24px; height:24px; border-radius:5px; }
.wp-chip-x { background:transparent; border:none; color: var(--text-dim); cursor:pointer; font-size:14px; line-height:1; padding:2px; }
.wp-chip-x:hover { color: var(--danger); }
.wp-search { position:relative; }
.wp-results { display:none; position:relative; margin-top:6px; max-height:260px; overflow-y:auto; background: var(--bg-soft); border:1px solid rgba(226,188,98,.25); border-radius:10px; padding:6px; }
.wp-result { display:flex; align-items:center; gap:10px; width:100%; text-align:left; background:transparent; border:none; color:var(--text); padding:7px 8px; border-radius:7px; cursor:pointer; font-size:13.5px; font-family:inherit; }
.wp-result:hover, .wp-result:focus-visible { background: var(--gold-soft); color:#fff; outline:none; }
.wp-result img { width:28px; height:28px; flex-shrink:0; }
.wp-empty { color: var(--text-dim); font-size:13px; padding:8px; }
.card-actions { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
"""

SHELL = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }} - {{ brand_name }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>{{ css|safe }}</style>
</head>
<body>
<header class="topbar">
  <a href="{{ url_for('home') }}" class="brand">{{ brand_name }} <span>&middot; painel do bot</span></a>
  <nav>
    <a href="{{ url_for('home') }}">Conteudos</a>
    <a href="{{ url_for('pings_painel') }}">Pings</a>
    <a href="{{ url_for('builds_lib_page') }}">Biblioteca de builds</a>
    <a href="{{ url_for('classes_page') }}">Roster simples</a>
  </nav>
</header>
<main>{{ content|safe }}</main>
<footer>
  {% if user_name %}Logado como {{ user_name }} (Discord) &middot; {% endif %}Painel administrativo &middot; Empório dos Bots
</footer>
<script>const ALL_WEAPONS = {{ weapons_json|safe }}; const RENDER_BASE = {{ render_base|tojson }};</script>
<script>{{ picker_js|safe }}</script>
</body>
</html>
"""


def _legacy_brand():
    """Nome de marca + usuario pro topo/rodape do painel legado: mostra o
    nome da guilda (ou do espaco pessoal) que esta ativa na sessao, e quem
    esta logado - nada fixo/hardcoded, tudo lido da sessao Discord real."""
    ctx = _legacy_context()
    if not ctx:
        return "Empório dos Bots", None
    session, guild_id = ctx
    try:
        workspaces = _usable_workspaces(session)
        ws = next((w for w in workspaces if w.get("id") == guild_id), None)
    except Exception:
        ws = None
    brand_name = (ws or {}).get("name") or "Empório dos Bots"
    user_name = session.get("global_name") or session.get("username")
    return brand_name, user_name


def shell(title, content):
    weapons_json = json.dumps(shared.load_items_db().get("mainhand", []), ensure_ascii=False)
    brand_name, user_name = _legacy_brand()
    return render_template_string(
        SHELL, title=title, content=content, css=BASE_CSS,
        weapons_json=weapons_json, render_base=shared.RENDER_BASE, picker_js=PICKER_JS,
        brand_name=brand_name, user_name=user_name,
    )


ERRO_MENSAGENS = {
    "campos_obrigatorios": "Preencha todos os campos obrigatorios antes de salvar.",
    "sem_build": "Escolha pelo menos uma arma/build (da biblioteca ou nova) antes de salvar.",
    "limite_classes": "Voce ja atingiu o limite de 25 classes no roster.",
}


def error_banner():
    """Le ?erro=<codigo> da URL (se tiver) e devolve o HTML do banner vermelho,
    ou string vazia se nao tiver erro. Usado nas paginas que voltam de um POST
    que falhou validacao - assim o formulario nao "some" sem explicar o motivo."""
    codigo = request.args.get("erro")
    if not codigo:
        return ""
    msg = ERRO_MENSAGENS.get(codigo, "Nao foi possivel salvar - confira os campos.")
    return f'<div class="banner error">&#9888; {msg}</div>'


def tip(texto):
    """Spanzinho 'i' com tooltip explicando um campo - usado nos labels dos
    formularios (fica ao lado do texto do label, ver .info-tip no CSS)."""
    safe = texto.replace('"', "&quot;")
    return f'<span class="info-tip" tabindex="0" data-tip="{safe}">i</span>'


PICKER_JS = """
function wpState(fieldId) {
  var raw = document.getElementById('wp-hidden-' + fieldId).value;
  if (!raw) return [];
  return raw.split(',').filter(Boolean).map(function(pair) {
    var i = pair.indexOf('::');
    return i === -1 ? {id: pair, nome: pair} : {id: pair.slice(0, i), nome: pair.slice(i + 2)};
  });
}
function wpSetState(fieldId, items) {
  document.getElementById('wp-hidden-' + fieldId).value = items.map(function(it) { return it.id + '::' + it.nome; }).join(',');
  wpRenderChips(fieldId, items);
}
function wpRenderChips(fieldId, items) {
  var wrap = document.getElementById('wp-chips-' + fieldId);
  wrap.innerHTML = '';
  items.forEach(function(it) {
    var chip = document.createElement('span');
    chip.className = 'wp-chip';
    var img = document.createElement('img');
    img.src = RENDER_BASE + '/' + it.id + '.png?count=1&quality=1'; img.alt = '';
    img.onerror = function() { img.style.opacity = 0.15; };
    var span = document.createElement('span'); span.textContent = it.nome;
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'wp-chip-x'; btn.setAttribute('aria-label', 'Remover ' + it.nome); btn.textContent = '\\u00d7';
    btn.addEventListener('click', function() { wpRemoveItem(fieldId, it.id); });
    chip.appendChild(img); chip.appendChild(span); chip.appendChild(btn);
    wrap.appendChild(chip);
  });
}
function wpRemoveItem(fieldId, id) {
  wpSetState(fieldId, wpState(fieldId).filter(function(it) { return it.id !== id; }));
}
function wpAddItem(fieldId, id, nome, multiple) {
  var items = wpState(fieldId);
  if (!multiple) items = [];
  if (!items.some(function(it) { return it.id === id; })) items.push({id: id, nome: nome});
  wpSetState(fieldId, items);
  var search = document.getElementById('wp-search-' + fieldId);
  search.value = '';
  document.getElementById('wp-results-' + fieldId).style.display = 'none';
  search.focus();
}
function wpFilter(fieldId, multiple) {
  var q = document.getElementById('wp-search-' + fieldId).value.trim().toLowerCase();
  var results = document.getElementById('wp-results-' + fieldId);
  var pool = q ? ALL_WEAPONS.filter(function(w) { return w.nome.toLowerCase().indexOf(q) !== -1; }) : ALL_WEAPONS;
  var matches = pool.slice(0, 30);
  results.innerHTML = '';
  if (!matches.length) {
    results.innerHTML = '<div class="wp-empty">Nenhuma arma encontrada.</div>';
  } else {
    matches.forEach(function(item) {
      var btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'wp-result'; btn.setAttribute('role', 'option');
      var img = document.createElement('img');
      img.src = RENDER_BASE + '/' + item.id + '.png?count=1&quality=1'; img.alt = ''; img.loading = 'lazy';
      var span = document.createElement('span'); span.textContent = item.nome;
      btn.appendChild(img); btn.appendChild(span);
      btn.addEventListener('click', function() { wpAddItem(fieldId, item.id, item.nome, multiple); });
      results.appendChild(btn);
    });
  }
  results.style.display = 'block';
}
document.addEventListener('click', function(e) {
  document.querySelectorAll('.wp-results').forEach(function(r) {
    if (r.style.display !== 'none' && r.parentElement && !r.parentElement.contains(e.target)) r.style.display = 'none';
  });
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    document.querySelectorAll('.wp-results').forEach(function(r) { r.style.display = 'none'; });
  }
});

function libToggle(fieldId, buildId, btn) {
  var hidden = document.getElementById('libpicker-hidden-' + fieldId);
  var ids = hidden.value ? hidden.value.split(',').filter(Boolean) : [];
  var idx = ids.indexOf(buildId);
  if (idx === -1) { ids.push(buildId); btn.classList.add('selected'); }
  else { ids.splice(idx, 1); btn.classList.remove('selected'); }
  hidden.value = ids.join(',');
}

function libToggleSingle(fieldId, buildId, btn) {
  var hidden = document.getElementById('libpicker-hidden-' + fieldId);
  var picker = btn.closest('.lib-picker');
  var wasSelected = btn.classList.contains('selected');
  picker.querySelectorAll('.lib-chip.selected').forEach(function(el) { el.classList.remove('selected'); });
  if (wasSelected) {
    hidden.value = '';
  } else {
    btn.classList.add('selected');
    hidden.value = buildId;
    // escolher uma build da biblioteca substitui qualquer arma digitada na busca livre
    if (document.getElementById('wp-hidden-' + fieldId)) wpSetState(fieldId, []);
  }
}
// campo de busca livre e picker de biblioteca (modo unico) sao mutuamente exclusivos:
// comecar a digitar uma arma nova desmarca a build da biblioteca escolhida antes.
// So vale pra pickers marcados '.single' (roster simples) - nao mexe no picker
// multiplo das composicoes, onde biblioteca + arma nova se somam de proposito.
document.addEventListener('input', function(e) {
  var t = e.target;
  if (!t.classList || !t.classList.contains('wp-search')) return;
  var fieldId = t.id.slice('wp-search-'.length);
  var picker = document.getElementById('libpicker-' + fieldId);
  if (!picker || !picker.classList.contains('single')) return;
  var hidden = document.getElementById('libpicker-hidden-' + fieldId);
  if (hidden) hidden.value = '';
  picker.querySelectorAll('.lib-chip.selected').forEach(function(el) { el.classList.remove('selected'); });
});
"""


def render_weapon_picker(field_id, hidden_name, multiple, initial, label):
    """Seletor de armas acessivel (label real, busca, resultados como <button>
    focaveis por teclado). 'initial' = lista de (item_id, nome) ja escolhidos.
    O campo escondido carrega 'id::nome,id2::nome2' - o backend ja sabe ler esse
    formato (ver albion_shared._parse_builds_texto / _parse_single_build_field)."""
    initial = [(iid, nome) for iid, nome in initial if iid]
    hidden_value = ",".join(f"{iid}::{nome}" for iid, nome in initial)
    chips = "".join(
        f'<span class="wp-chip"><img src="{shared.RENDER_BASE}/{iid}.png?count=1&quality=1" alt="">'
        f'<span>{nome}</span>'
        f'<button type="button" class="wp-chip-x" aria-label="Remover {nome}" '
        f'onclick="wpRemoveItem(\'{field_id}\',\'{iid}\')">&times;</button></span>'
        for iid, nome in initial
    )
    multiple_js = "true" if multiple else "false"
    return f'''<div class="wp">
  <label for="wp-search-{field_id}">{label}</label>
  <input type="hidden" name="{hidden_name}" id="wp-hidden-{field_id}" value="{hidden_value}">
  <div class="wp-chips" id="wp-chips-{field_id}">{chips}</div>
  <input type="text" id="wp-search-{field_id}" class="wp-search" autocomplete="off"
         placeholder="Digite pra buscar uma arma..." role="combobox" aria-expanded="false"
         aria-controls="wp-results-{field_id}" aria-autocomplete="list"
         oninput="wpFilter('{field_id}', {multiple_js})" onfocus="wpFilter('{field_id}', {multiple_js})">
  <div class="wp-results" id="wp-results-{field_id}" role="listbox" aria-label="Resultados da busca de armas"></div>
</div>'''


def render_lib_picker_single(field_id, library, selected_id=None, hidden_name="build_lib"):
    """Picker de build da biblioteca com selecao UNICA (tipo radio) - usado onde
    so faz sentido 1 build por vez (ex: Roster Simples, que e 1 build fixa sem
    swap). Clicar de novo no chip marcado desmarca (ai o campo de busca abaixo
    assume). 'selected_id' pre-marca o chip atual quando ta editando."""
    chips = "".join(
        f'<button type="button" class="build-chip lib-chip{" selected" if b["id"] == selected_id else ""}" '
        f'data-id="{b["id"]}" onclick="libToggleSingle(\'{field_id}\',\'{b["id"]}\', this)">'
        + (f'<img src="{shared.RENDER_BASE}/{b["item_id"]}.png?count=1&quality=1" alt="" loading="lazy">'
           if b.get("item_id") else '<div class="noicon">?</div>')
        + f'<span>{b["nome"]}</span><span class="check">&check;</span></button>'
        for b in library
    )
    empty = "" if library else '<span class="lib-empty">Nenhuma build salva ainda - crie uma abaixo e ela ja entra pra biblioteca.</span>'
    return (
        f'<div class="lib-picker single" id="libpicker-{field_id}">{chips}{empty}</div>'
        f'<input type="hidden" name="{hidden_name}" id="libpicker-hidden-{field_id}" value="{selected_id or ""}">'
    )


def _sync_weapon_to_library(item_id, nome):
    """Garante que uma arma digitada na busca livre (fora da biblioteca) vire uma
    build salva, deduplicada por item_id - assim ela fica disponivel como opcao
    pronta em qualquer classe/composicao depois, sem passo extra do usuario."""
    if not item_id:
        return
    library = shared.load_build_library()
    if any(b.get("item_id") == item_id for b in library):
        return
    novo_id, sufixo = shared._slug(nome), 2
    ids_existentes = {b["id"] for b in library}
    while novo_id in ids_existentes:
        novo_id = f"{shared._slug(nome)}_{sufixo}"
        sufixo += 1
    library.append({"id": novo_id, "nome": nome, "item_id": item_id, "arma_nome": nome, "emoji_id": None, "equip": {}})
    shared.save_build_library(library)


def render_icon(build):
    item_id = build.get("item_id")
    nome = build.get("nome", "?")
    if item_id:
        url = f"{shared.RENDER_BASE}/{item_id}.png?count=1&quality=1"
        return f'<div class="build-chip"><img src="{url}" alt="{nome}" loading="lazy"><span>{nome}</span></div>'
    return f'<div class="build-chip"><div class="noicon">?</div><span>{nome}</span></div>'


# =========================================================================
# HOME - lista de conteudos
# =========================================================================

HOME_TPL = """
<h1>Conteudos</h1>
<p class="subtitle">Cada conteudo agrupa as composicoes (comps) usadas nele. Escolha um pra ver/editar as builds, ou crie um novo.</p>

<div class="grid">
  {% for c in content_types %}
  <a class="card link" href="{{ url_for('conteudo_page', content_id=c['id']) }}">
    <h3>{{ c['nome'] }}</h3>
    <div class="meta">{{ c['_count'] }} composicao(oes)</div>
    <span class="pill">ver builds &rarr;</span>
  </a>
  {% endfor %}

  <div class="card new-card">
    <form method="post" action="{{ url_for('novo_conteudo') }}" class="field" style="width:100%">
      <label>Novo conteudo</label>
      <div style="display:flex; gap:8px;">
        <input type="text" name="nome" placeholder="Ex: Corridas de Nucleo" required>
        <button class="btn" type="submit">+</button>
      </div>
    </form>
  </div>
</div>

{% if sem_categoria_count %}
<h2>Sem categoria</h2>
<p class="subtitle">Composicoes que ainda nao foram encaixadas em um conteudo.</p>
<div class="grid">
  <a class="card link" href="{{ url_for('conteudo_page', content_id='_sem') }}">
    <h3>Sem categoria</h3>
    <div class="meta">{{ sem_categoria_count }} composicao(oes)</div>
    <span class="pill">ver &rarr;</span>
  </a>
</div>
{% endif %}
"""


@app.route("/")
def home():
    content_types = shared.load_content_types()
    templates = shared.load_templates()
    for c in content_types:
        c["_count"] = sum(1 for t in templates if t.get("conteudo_id") == c["id"])
    sem_categoria_count = sum(1 for t in templates if not t.get("conteudo_id"))
    html = render_template_string(HOME_TPL, content_types=content_types, sem_categoria_count=sem_categoria_count)
    return shell("Conteudos", html)


@app.route("/conteudo/novo", methods=["POST"])
def novo_conteudo():
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return redirect(url_for("home"))
    content_types = shared.load_content_types()
    novo_id = shared._slug(nome)
    if any(c["id"] == novo_id for c in content_types):
        return redirect(url_for("home"))
    content_types.append({"id": novo_id, "nome": nome, "ordem": len(content_types) + 1})
    shared.save_content_types(content_types)
    return redirect(url_for("conteudo_page", content_id=novo_id))


@app.route("/conteudo/<content_id>/excluir", methods=["POST"])
def excluir_conteudo(content_id):
    content_types = [c for c in shared.load_content_types() if c["id"] != content_id]
    shared.save_content_types(content_types)
    templates = shared.load_templates()
    for t in templates:
        if t.get("conteudo_id") == content_id:
            t["conteudo_id"] = None
    shared.save_templates(templates)
    return redirect(url_for("home"))


# =========================================================================
# CONTEUDO - lista de composicoes dentro dele
# =========================================================================

CONTEUDO_TPL = """
<div class="crumb"><a href="{{ url_for('home') }}">Conteudos</a> / {{ nome }}</div>
<div class="flex-between">
  <h1>{{ nome }}</h1>
  {% if content_id != '_sem' %}
  <form method="post" action="{{ url_for('excluir_conteudo', content_id=content_id) }}" onsubmit="return confirm('Excluir esse conteudo? As composicoes ficam sem categoria, nao sao apagadas.');">
    <button class="btn danger small" type="submit">Excluir conteudo</button>
  </form>
  {% endif %}
</div>
<p class="subtitle">Composicoes cadastradas nesse conteudo.</p>

<div class="grid">
  {% for t in templates %}
  <a class="card link" href="{{ url_for('comp_page', comp_id=t['id']) }}">
    <h3>{{ t['nome'] }}</h3>
    <div class="meta">{{ t['slots']|length }} slot(s)</div>
  </a>
  {% endfor %}

  <div class="card new-card">
    <form method="post" action="{{ url_for('novo_comp', content_id=content_id) }}" class="field" style="width:100%">
      <label>Nova composicao</label>
      <div style="display:flex; gap:8px;">
        <input type="text" name="nome" placeholder="Ex: Comp 5v5 Padrao" required>
        <button class="btn" type="submit">+</button>
      </div>
    </form>
  </div>
</div>
"""


@app.route("/conteudo/<content_id>")
def conteudo_page(content_id):
    templates = shared.load_templates()
    if content_id == SEM_CATEGORIA:
        nome = "Sem categoria"
        filtrados = [t for t in templates if not t.get("conteudo_id")]
    else:
        content_types = shared.load_content_types()
        tipo = next((c for c in content_types if c["id"] == content_id), None)
        if not tipo:
            abort(404)
        nome = tipo["nome"]
        filtrados = [t for t in templates if t.get("conteudo_id") == content_id]
    html = render_template_string(CONTEUDO_TPL, nome=nome, content_id=content_id, templates=filtrados)
    return shell(nome, html)


@app.route("/conteudo/<content_id>/comp/novo", methods=["POST"])
def novo_comp(content_id):
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return redirect(url_for("conteudo_page", content_id=content_id))
    templates = shared.load_templates()
    novo_id = shared._slug(nome)
    sufixo = 2
    base_id = novo_id
    while any(t["id"] == novo_id for t in templates):
        novo_id = f"{base_id}_{sufixo}"
        sufixo += 1
    templates.append({
        "id": novo_id, "nome": nome,
        "conteudo_id": None if content_id == SEM_CATEGORIA else content_id,
        "slots": [],
    })
    shared.save_templates(templates)
    return redirect(url_for("comp_page", comp_id=novo_id))


# =========================================================================
# COMPOSICAO - editor de slots/builds
# =========================================================================

COMP_TPL = """
<div class="crumb"><a href="{{ url_for('home') }}">Conteudos</a> / <a href="{{ url_for('conteudo_page', content_id=conteudo_atual) }}">{{ conteudo_nome }}</a> / {{ comp['nome'] }}</div>

<div class="flex-between">
  <h1>{{ comp['nome'] }}</h1>
  <form method="post" action="{{ url_for('excluir_comp', comp_id=comp['id']) }}" onsubmit="return confirm('Excluir essa composicao inteira?');">
    <button class="btn danger small" type="submit">Excluir composicao</button>
  </form>
</div>

{{ error_banner|safe }}

<div class="box">
  <h3>Dados da composicao</h3>
  <form method="post" action="{{ url_for('editar_comp', comp_id=comp['id']) }}">
    <div class="row2">
      <div class="field">
        <label>Nome {{ tip_nome_comp|safe }}</label>
        <input type="text" name="nome" value="{{ comp['nome'] }}" required>
      </div>
      <div class="field">
        <label>Conteudo {{ tip_conteudo_comp|safe }}</label>
        <select name="conteudo_id">
          <option value="">Sem categoria</option>
          {% for c in content_types %}
          <option value="{{ c['id'] }}" {% if comp.get('conteudo_id') == c['id'] %}selected{% endif %}>{{ c['nome'] }}</option>
          {% endfor %}
        </select>
      </div>
    </div>
    <button class="btn secondary small" type="submit">Salvar</button>
  </form>
</div>

<h2>Slots ({{ comp['slots']|length }})</h2>

{% if not comp['slots'] %}
<div class="empty">Nenhum slot ainda. Adicione o primeiro abaixo.</div>
{% endif %}

{% for s in comp['slots'] %}
<div class="slot-card">
  <div class="slot-head">
    <div>
      <h3>{{ s['nome'] }}</h3>
      <div class="meta">Cargo: {{ s['cargo'] }} &middot; Vagas: {{ s.get('vagas') or 'sem limite' }}</div>
    </div>
    <form method="post" action="{{ url_for('excluir_slot', comp_id=comp['id'], slot_id=s['id']) }}" onsubmit="return confirm('Remover esse slot?');">
      <button class="btn danger small" type="submit">Remover</button>
    </form>
  </div>
  <div class="builds-row">
    {% for b in s['builds'] %}
    <a class="build-chip link" href="{{ url_for('build_equip_page', comp_id=comp['id'], slot_id=s['id'], build_idx=loop.index0) }}">
      {% if b.get('item_id') %}
      <img src="{{ render_base }}/{{ b['item_id'] }}.png?count=1&quality=1" alt="{{ b['nome'] }}" loading="lazy">
      {% else %}
      <div class="noicon">?</div>
      {% endif %}
      <span>{{ b['nome'] }}<span class="editar">montar equipamento &rarr;</span></span>
    </a>
    {% endfor %}
  </div>
  {% if build_library %}
  <form method="post" action="{{ url_for('usar_build_biblioteca', comp_id=comp['id'], slot_id=s['id']) }}" class="inline" style="margin-top:12px; align-items:center;">
    <label for="lib-{{ s['id'] }}" style="margin:0; white-space:nowrap;">Usar da biblioteca:</label>
    <select name="build_id" id="lib-{{ s['id'] }}" style="width:auto; flex:1;">
      {% for lb in build_library %}
      <option value="{{ lb['id'] }}">{{ lb['nome'] }}</option>
      {% endfor %}
    </select>
    <button class="btn secondary small" type="submit">+ Adicionar</button>
  </form>
  {% endif %}
</div>
{% endfor %}

<div class="box">
  <h3>+ Adicionar slot</h3>
  <p class="hint">Um slot e uma funcao dentro da composicao (ex: Main Tank, Healer 1). Depois de criar, escolha o equipamento completo clicando na build.</p>
  <form method="post" action="{{ url_for('novo_slot', comp_id=comp['id']) }}">
    <div class="row3">
      <div class="field">
        <label>Nome do slot {{ tip_nome_slot|safe }}</label>
        <input type="text" name="nome" id="novoslot-nome" placeholder="Ex: Main Tank" required oninput="if(!window.__cargoTocado) document.getElementById('novoslot-cargo').value = this.value;">
      </div>
      <div class="field">
        <label>Cargo do Discord {{ tip_cargo_slot|safe }}</label>
        <input type="text" name="cargo" id="novoslot-cargo" placeholder="Ex: Main Tank" required oninput="window.__cargoTocado = true;">
      </div>
      <div class="field">
        <label>Vagas {{ tip_vagas_slot|safe }}</label>
        <input type="number" name="vagas" min="1" placeholder="sem limite">
      </div>
    </div>

    <div class="section-label">Builds desse slot {{ tip_builds_slot|safe }}<span class="count">escolha pelo menos uma</span></div>

    <div class="field">
      <label style="margin-bottom:8px;">Da biblioteca ({{ build_library|length }} disponiveis)</label>
      <div class="lib-picker" id="libpicker-novoslot">
        {% for b in build_library %}
        <button type="button" class="build-chip lib-chip" data-id="{{ b['id'] }}" onclick="libToggle('novoslot','{{ b['id'] }}', this)">
          {% if b.get('item_id') %}
          <img src="{{ render_base }}/{{ b['item_id'] }}.png?count=1&quality=1" alt="" loading="lazy">
          {% else %}
          <div class="noicon">?</div>
          {% endif %}
          <span>{{ b['nome'] }}</span>
          <span class="check">&check;</span>
        </button>
        {% endfor %}
        {% if not build_library %}
        <span class="lib-empty">Nenhuma build salva ainda - crie uma abaixo, ela entra pra biblioteca automaticamente.</span>
        {% endif %}
      </div>
      <input type="hidden" name="builds_lib" id="libpicker-hidden-novoslot" value="">
    </div>

    <div class="or-divider">ou crie uma nova</div>

    <div class="field">
      {{ builds_picker_html|safe }}
      <div class="meta" style="margin-top:6px;">Escolha uma ou mais armas - cada uma vira uma build nova, ja salva direto na Biblioteca de builds pra reusar depois em qualquer composicao.</div>
    </div>
    <button class="btn" type="submit">Adicionar slot</button>
  </form>
</div>
"""


def _find_comp_or_404(comp_id):
    templates = shared.load_templates()
    comp = next((t for t in templates if t["id"] == comp_id), None)
    if not comp:
        abort(404)
    return templates, comp


@app.route("/comp/<comp_id>")
def comp_page(comp_id):
    templates, comp = _find_comp_or_404(comp_id)
    content_types = shared.load_content_types()
    conteudo_atual = comp.get("conteudo_id") or SEM_CATEGORIA
    conteudo_nome = "Sem categoria"
    if comp.get("conteudo_id"):
        tipo = next((c for c in content_types if c["id"] == comp["conteudo_id"]), None)
        conteudo_nome = tipo["nome"] if tipo else "Sem categoria"
    builds_picker_html = render_weapon_picker("novoslot", "builds", True, [], "Builds (armas)")
    html = render_template_string(
        COMP_TPL, comp=comp, content_types=content_types,
        conteudo_atual=conteudo_atual, conteudo_nome=conteudo_nome, render_icon=render_icon,
        render_base=shared.RENDER_BASE, builds_picker_html=builds_picker_html,
        build_library=shared.load_build_library(),
        error_banner=error_banner(),
        tip_nome_comp=tip("Nome interno da composicao - so aparece aqui no painel e quando o /ping-conteudo mostra a composicao de referencia."),
        tip_conteudo_comp=tip("Categoria pra organizar as composicoes no menu (ex: ZvZ, Avalon, Hellgate). So serve pra agrupar, nao muda o funcionamento do bot."),
        tip_nome_slot=tip("Nome que aparece no card da build (ex: Main Tank, Healer 1, DPS 2). Pode repetir numeros se tiver mais de uma vaga igual."),
        tip_cargo_slot=tip("Nome do cargo (role) do Discord ligado a essa funcao. Se a guilda nao usa cargos separados por funcao, so repita o nome do slot."),
        tip_vagas_slot=tip("Quantas pessoas podem ocupar essa funcao ao mesmo tempo. Deixe em branco pra nao ter limite."),
        tip_builds_slot=tip("Cada build vira uma opcao pra quem pegar esse slot (util pra swap - ex: arco OU besta). De pra combinar builds da biblioteca com builds novas na mesma vez."),
    )
    return shell(comp["nome"], html)


@app.route("/comp/<comp_id>/editar", methods=["POST"])
def editar_comp(comp_id):
    templates, comp = _find_comp_or_404(comp_id)
    nome = (request.form.get("nome") or "").strip()
    conteudo_id = (request.form.get("conteudo_id") or "").strip() or None
    if nome:
        comp["nome"] = nome
    comp["conteudo_id"] = conteudo_id
    shared.save_templates(templates)
    return redirect(url_for("comp_page", comp_id=comp_id))


@app.route("/comp/<comp_id>/excluir", methods=["POST"])
def excluir_comp(comp_id):
    templates = shared.load_templates()
    comp = next((t for t in templates if t["id"] == comp_id), None)
    conteudo_id = comp.get("conteudo_id") if comp else None
    templates = [t for t in templates if t["id"] != comp_id]
    shared.save_templates(templates)
    return redirect(url_for("conteudo_page", content_id=conteudo_id or SEM_CATEGORIA))


@app.route("/comp/<comp_id>/slot/novo", methods=["POST"])
def novo_slot(comp_id):
    templates, comp = _find_comp_or_404(comp_id)
    nome = (request.form.get("nome") or "").strip()
    cargo = (request.form.get("cargo") or "").strip()
    vagas_raw = (request.form.get("vagas") or "").strip()
    builds_raw = (request.form.get("builds") or "").strip()
    builds_lib_raw = (request.form.get("builds_lib") or "").strip()
    if not nome or not cargo:
        return redirect(url_for("comp_page", comp_id=comp_id, erro="campos_obrigatorios"))
    if not builds_raw and not builds_lib_raw:
        return redirect(url_for("comp_page", comp_id=comp_id, erro="sem_build"))

    # builds novas (digitadas/escolhidas no seletor de armas) - cada uma ja
    # entra direto na biblioteca compartilhada tambem, pra poder reusar em
    # qualquer outra composicao sem precisar buscar a arma de novo.
    builds_novas = shared._parse_builds_texto(builds_raw)
    for b in builds_novas:
        if not b.get("item_id") and b.get("arma_nome"):
            b["item_id"] = shared.resolve_item_id_by_name(b["arma_nome"])

    if builds_novas:
        library = shared.load_build_library()
        item_ids_na_biblioteca = {b.get("item_id") for b in library if b.get("item_id")}
        for b in builds_novas:
            if b.get("item_id") and b["item_id"] not in item_ids_na_biblioteca:
                novo_lib_id, sufixo = shared._slug(b["nome"]), 2
                ids_existentes = {x["id"] for x in library}
                while novo_lib_id in ids_existentes:
                    novo_lib_id = f"{shared._slug(b['nome'])}_{sufixo}"
                    sufixo += 1
                library.append({
                    "id": novo_lib_id, "nome": b["nome"], "item_id": b["item_id"],
                    "arma_nome": b.get("arma_nome"), "emoji_id": None, "equip": {},
                })
                item_ids_na_biblioteca.add(b["item_id"])
        shared.save_build_library(library)

    # builds escolhidas da biblioteca (predefinidas) - copia sem o "id" porque
    # dentro do slot elas sao endereçadas por indice, igual usar_build_biblioteca faz
    library_by_id = {b["id"]: b for b in shared.load_build_library()}
    builds_da_biblioteca = []
    for build_id in builds_lib_raw.split(","):
        build_id = build_id.strip()
        lib_build = library_by_id.get(build_id)
        if lib_build:
            copia = copy.deepcopy(lib_build)
            copia.pop("id", None)
            builds_da_biblioteca.append(copia)

    builds = builds_da_biblioteca + builds_novas

    novo = {
        "id": shared._slug(nome), "nome": nome, "cargo": cargo,
        "vagas": int(vagas_raw) if vagas_raw.isdigit() else None,
        "ordem": len(comp["slots"]) + 1, "role_id": None,
        "item_id": builds[0].get("item_id") if builds else None,
        "emoji_id": None,
        "builds": builds,
    }
    comp["slots"].append(novo)
    shared.save_templates(templates)
    return redirect(url_for("comp_page", comp_id=comp_id))


@app.route("/comp/<comp_id>/slot/<slot_id>/excluir", methods=["POST"])
def excluir_slot(comp_id, slot_id):
    templates, comp = _find_comp_or_404(comp_id)
    comp["slots"] = [s for s in comp["slots"] if s["id"] != slot_id]
    shared.save_templates(templates)
    return redirect(url_for("comp_page", comp_id=comp_id))


@app.route("/comp/<comp_id>/slot/<slot_id>/build/da_biblioteca", methods=["POST"])
def usar_build_biblioteca(comp_id, slot_id):
    templates, comp = _find_comp_or_404(comp_id)
    slot = next((s for s in comp["slots"] if s["id"] == slot_id), None)
    if not slot:
        abort(404)
    build_id = (request.form.get("build_id") or "").strip()
    lib_build = next((b for b in shared.load_build_library() if b["id"] == build_id), None)
    if lib_build:
        novo = copy.deepcopy(lib_build)
        novo.pop("id", None)  # builds dentro de um slot sao endereçadas por indice, nao por id
        slot.setdefault("builds", []).append(novo)
        if len(slot["builds"]) == 1:
            slot["item_id"] = novo.get("item_id")
        shared.save_templates(templates)
    return redirect(url_for("comp_page", comp_id=comp_id))


# =========================================================================
# EQUIPAMENTO - paperdoll visual de uma build (clicar/arrastar itens pros quadrados)
# =========================================================================

EQUIP_TPL = """
<div class="crumb">
  {% for label, u in crumb_items %}{% if u %}<a href="{{ u }}">{{ label }}</a>{% else %}{{ label }}{% endif %}{% if not loop.last %} / {% endif %}{% endfor %}
</div>
<h1>{{ page_title }}</h1>
<p class="subtitle">{{ subtitle }}</p>

<form method="post" action="{{ save_url }}" id="equip-form">
  <div class="paperdoll" id="paperdoll">
    {% for sd in equip_slots %}
    {% set ev = equip.get(sd['key'], {}) %}
    {% set current = ev.get('item') %}
    {% set cur_swaps = ev.get('swaps', []) %}
    <div class="equip-cell"{% if sd.get('own_row') %} style="grid-column: 2;"{% endif %}>
      <div class="equip-slot" data-slot="{{ sd['key'] }}" data-label="{{ sd['label'] }}" onclick="openPalette('{{ sd['key'] }}','primary')">
        <div class="equip-slot-inner" id="slot-{{ sd['key'] }}">
          {% if current %}
          <img src="{{ render_base }}/{{ current }}.png?count=1&quality=1" title="{{ items_by_id.get(current, current) }}" onerror="this.style.opacity=0.15">
          {% else %}
          <span class="plus">+</span>
          {% endif %}
        </div>
        <button type="button" class="equip-clear" aria-label="Remover {{ sd['label'] }}" onclick="event.stopPropagation(); clearPrimary('{{ sd['key'] }}')" title="Remover">&times;</button>
      </div>
      <div class="equip-slot-label">{{ sd['label'] }}</div>
      <div class="equip-slot-abilities" id="abilities-{{ sd['key'] }}">
        {% set hab = abilities_initial.get(sd['key']) %}
        {% if hab %}{% for h in hab['habilidades'] %}<span class="hab-chip">{{ h['slot'] }} · {{ h['nome'] }}</span>{% endfor %}{% endif %}
      </div>
      <div class="swap-row" id="swaps-{{ sd['key'] }}" data-slot="{{ sd['key'] }}">
        {% for sw in cur_swaps %}
        <span class="swap-chip" title="{{ items_by_id.get(sw, sw) }}">
          <img src="{{ render_base }}/{{ sw }}.png?count=1&quality=1" alt="{{ items_by_id.get(sw, sw) }}">
          <button type="button" class="swap-x" aria-label="Remover swap {{ items_by_id.get(sw, sw) }}" onclick="removeSwap('{{ sd['key'] }}','{{ sw }}')">&times;</button>
        </span>
        {% endfor %}
        <button type="button" class="swap-add" aria-label="Adicionar swap de {{ sd['label'] }}" onclick="openPalette('{{ sd['key'] }}','swap')">+</button>
      </div>
      <input type="hidden" name="{{ sd['key'] }}" id="hidden-{{ sd['key'] }}" value="{{ current or '' }}">
      <input type="hidden" name="{{ sd['key'] }}__swaps" id="hidden-{{ sd['key'] }}-swaps" value="{{ cur_swaps|join(',') }}">
    </div>
    {% endfor %}
  </div>
  <button class="btn" type="submit" style="margin-top:6px;">Salvar equipamento</button>
</form>

<p class="meta" style="margin-top:16px;">Clique no quadrado grande pra escolher o item principal. Clique no "+" pequeno embaixo dele pra adicionar um <strong>swap</strong> (alternativa da mesma categoria pra trocar no meio da luta, tipo outra arma ou offhand). Da pra arrastar da lista tambem. Passe o mouse pra ver o nome de qualquer item.</p>

<div class="box" id="palette-box">
  <h3 id="palette-title">Escolher item</h3>
  <label for="palette-search">Buscar item</label>
  <input type="text" id="palette-search" placeholder="Digite pra buscar..." oninput="filterPalette()" autocomplete="off">
  <div class="palette-grid" id="palette-grid" role="listbox" aria-label="Itens disponiveis"></div>
</div>

{{ extra_actions_html|safe }}

<script>
const ITEMS_DB = {{ items_db_json|safe }};
const ITEM_ABILITIES = {{ item_abilities_json|safe }};
const ITEMS_BY_ID = {};
Object.keys(ITEMS_DB).forEach(function(cat) { ITEMS_DB[cat].forEach(function(it) { ITEMS_BY_ID[it.id] = it.nome; }); });
let activeSlot = null;
let activeMode = 'primary';

function nameOf(id) { return ITEMS_BY_ID[id] || id; }

function familyOf(id) { return id ? id.replace(/^T\\d+_/, '').replace(/@\\d+$/, '') : null; }

function renderAbilities(slotKey, id) {
  const el = document.getElementById('abilities-' + slotKey);
  if (!el) return;
  const fam = familyOf(id);
  const info = fam ? ITEM_ABILITIES[fam] : null;
  el.innerHTML = '';
  if (info) {
    info.habilidades.forEach(function(h) {
      const span = document.createElement('span');
      span.className = 'hab-chip';
      span.textContent = h.slot + ' \\u00b7 ' + h.nome;
      el.appendChild(span);
    });
  }
}

function openPalette(slotKey, mode) {
  activeSlot = slotKey; activeMode = mode;
  document.querySelectorAll('.equip-slot').forEach(function(el) { el.classList.toggle('active', el.dataset.slot === slotKey); });
  const label = document.querySelector('.equip-slot[data-slot="' + slotKey + '"]').dataset.label;
  document.getElementById('palette-title').textContent = (mode === 'swap' ? 'Adicionar swap: ' : 'Escolher: ') + label;
  document.getElementById('palette-box').style.display = 'block';
  document.getElementById('palette-search').value = '';
  renderPalette(ITEMS_DB[slotKey] || []);
  document.getElementById('palette-search').focus();
}

function renderPalette(items) {
  const grid = document.getElementById('palette-grid');
  grid.innerHTML = '';
  if (!items.length) { grid.innerHTML = '<div class="empty" style="padding:14px;">Nenhum item nessa categoria ainda.</div>'; return; }
  items.forEach(function(it) {
    const div = document.createElement('div');
    div.className = 'palette-item';
    div.draggable = true;
    div.title = it.nome;
    div.setAttribute('role', 'option');
    div.tabIndex = 0;
    const img = document.createElement('img');
    img.src = RENDER_BASE + '/' + it.id + '.png?count=1&quality=1'; img.alt = '';
    img.onerror = function() { img.style.opacity = 0.15; };
    const span = document.createElement('span'); span.textContent = it.nome;
    div.appendChild(img); div.appendChild(span);
    div.addEventListener('click', function() { chooseItem(it.id); });
    div.addEventListener('keydown', function(e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); chooseItem(it.id); } });
    div.addEventListener('dragstart', function(e) { e.dataTransfer.setData('text/plain', activeSlot + '||' + it.id); });
    grid.appendChild(div);
  });
}

function filterPalette() {
  const q = document.getElementById('palette-search').value.trim().toLowerCase();
  const items = (ITEMS_DB[activeSlot] || []).filter(function(it) { return it.nome.toLowerCase().indexOf(q) !== -1; });
  renderPalette(items);
}

function chooseItem(id) {
  if (!activeSlot) return;
  if (activeMode === 'swap') addSwap(activeSlot, id); else setPrimary(activeSlot, id);
}

function setPrimary(slotKey, id) {
  document.getElementById('hidden-' + slotKey).value = id;
  document.getElementById('slot-' + slotKey).innerHTML = '<img src="' + RENDER_BASE + '/' + id + '.png?count=1&quality=1" title="' + nameOf(id).replace(/"/g, '') + '">';
  renderAbilities(slotKey, id);
}

function clearPrimary(slotKey) {
  document.getElementById('hidden-' + slotKey).value = '';
  document.getElementById('slot-' + slotKey).innerHTML = '<span class="plus">+</span>';
  renderAbilities(slotKey, null);
}

function getSwaps(slotKey) {
  const raw = document.getElementById('hidden-' + slotKey + '-swaps').value;
  return raw ? raw.split(',').filter(Boolean) : [];
}

function setSwaps(slotKey, ids) {
  document.getElementById('hidden-' + slotKey + '-swaps').value = ids.join(',');
  renderSwaps(slotKey, ids);
}

function renderSwaps(slotKey, ids) {
  const row = document.getElementById('swaps-' + slotKey);
  row.querySelectorAll('.swap-chip').forEach(function(el) { el.remove(); });
  const addBtn = row.querySelector('.swap-add');
  ids.forEach(function(id) {
    const nome = nameOf(id);
    const chip = document.createElement('span');
    chip.className = 'swap-chip'; chip.title = nome;
    const img = document.createElement('img');
    img.src = RENDER_BASE + '/' + id + '.png?count=1&quality=1'; img.alt = nome;
    img.onerror = function() { img.style.opacity = 0.15; };
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'swap-x'; btn.setAttribute('aria-label', 'Remover swap ' + nome); btn.textContent = '\\u00d7';
    btn.addEventListener('click', function() { removeSwap(slotKey, id); });
    chip.appendChild(img); chip.appendChild(btn);
    row.insertBefore(chip, addBtn);
  });
}

function addSwap(slotKey, id) {
  const ids = getSwaps(slotKey);
  if (!ids.includes(id)) ids.push(id);
  setSwaps(slotKey, ids);
}

function removeSwap(slotKey, id) {
  setSwaps(slotKey, getSwaps(slotKey).filter(function(x) { return x !== id; }));
}

document.querySelectorAll('.equip-slot').forEach(function(el) {
  el.addEventListener('dragover', function(e) { e.preventDefault(); el.classList.add('dragover'); });
  el.addEventListener('dragleave', function() { el.classList.remove('dragover'); });
  el.addEventListener('drop', function(e) {
    e.preventDefault(); el.classList.remove('dragover');
    const data = e.dataTransfer.getData('text/plain'); if (!data) return;
    const parts = data.split('||'); const fromSlot = parts[0], id = parts[1];
    if (fromSlot !== el.dataset.slot) return;
    setPrimary(el.dataset.slot, id);
    activeSlot = el.dataset.slot; activeMode = 'primary';
    document.querySelectorAll('.equip-slot').forEach(function(x) { x.classList.toggle('active', x.dataset.slot === el.dataset.slot); });
  });
});
document.querySelectorAll('.swap-row').forEach(function(row) {
  row.addEventListener('dragover', function(e) { e.preventDefault(); row.classList.add('dragover'); });
  row.addEventListener('dragleave', function() { row.classList.remove('dragover'); });
  row.addEventListener('drop', function(e) {
    e.preventDefault(); row.classList.remove('dragover');
    const data = e.dataTransfer.getData('text/plain'); if (!data) return;
    const parts = data.split('||'); const fromSlot = parts[0], id = parts[1];
    if (fromSlot !== row.dataset.slot) return;
    addSwap(row.dataset.slot, id);
  });
});
</script>
"""


def _render_equip_page(build, save_url, crumb_items, page_title, subtitle, extra_actions_html, title_tab):
    equip = shared.get_build_equip(build)
    items_db = shared.load_items_db()
    items_by_id = {it["id"]: it["nome"] for cat in items_db.values() for it in cat}
    abilities_initial = {
        sd["key"]: shared.get_item_abilities(equip.get(sd["key"], {}).get("item"))
        for sd in shared.EQUIP_SLOTS
    }
    html = render_template_string(
        EQUIP_TPL, equip_slots=shared.EQUIP_SLOTS, equip=equip, render_base=shared.RENDER_BASE,
        items_by_id=items_by_id, items_db_json=json.dumps(items_db, ensure_ascii=False),
        item_abilities_json=json.dumps(shared.ITEM_ABILITIES, ensure_ascii=False),
        abilities_initial=abilities_initial,
        save_url=save_url, crumb_items=crumb_items, page_title=page_title, subtitle=subtitle,
        extra_actions_html=extra_actions_html,
    )
    return shell(title_tab, html)


def _parse_equip_form():
    equip = {}
    for sd in shared.EQUIP_SLOTS:
        item = (request.form.get(sd["key"]) or "").strip() or None
        swaps_raw = (request.form.get(sd["key"] + "__swaps") or "").strip()
        swaps = [s for s in swaps_raw.split(",") if s.strip()]
        if item or swaps:
            equip[sd["key"]] = {"item": item, "swaps": swaps}
    return equip


def _find_build_or_404(comp_id, slot_id, build_idx):
    templates, comp = _find_comp_or_404(comp_id)
    slot = next((s for s in comp["slots"] if s["id"] == slot_id), None)
    if not slot or build_idx < 0 or build_idx >= len(slot.get("builds") or []):
        abort(404)
    return templates, comp, slot, slot["builds"][build_idx]


@app.route("/comp/<comp_id>/slot/<slot_id>/build/<int:build_idx>")
def build_equip_page(comp_id, slot_id, build_idx):
    templates, comp, slot, build = _find_build_or_404(comp_id, slot_id, build_idx)
    content_types = shared.load_content_types()
    conteudo_nome = "Sem categoria"
    if comp.get("conteudo_id"):
        tipo = next((c for c in content_types if c["id"] == comp["conteudo_id"]), None)
        conteudo_nome = tipo["nome"] if tipo else "Sem categoria"

    crumb_items = [
        ("Conteudos", url_for("home")),
        (conteudo_nome, url_for("conteudo_page", content_id=comp.get("conteudo_id") or SEM_CATEGORIA)),
        (comp["nome"], url_for("comp_page", comp_id=comp["id"])),
        (build["nome"], None),
    ]
    save_url = url_for("salvar_build_equip", comp_id=comp_id, slot_id=slot_id, build_idx=build_idx)
    salvar_lib_url = url_for("salvar_na_biblioteca", comp_id=comp_id, slot_id=slot_id, build_idx=build_idx)
    extra = (
        f'<form method="post" action="{salvar_lib_url}" class="inline" style="margin-top:14px;">'
        f'<button class="btn secondary small" type="submit">Salvar essa build na biblioteca</button></form>'
    )
    return _render_equip_page(
        build, save_url, crumb_items, build["nome"],
        slot["nome"] + " · clique num quadrado pra montar o equipamento principal e os swaps.",
        extra, build["nome"] + " - " + slot["nome"],
    )


@app.route("/comp/<comp_id>/slot/<slot_id>/build/<int:build_idx>/salvar", methods=["POST"])
def salvar_build_equip(comp_id, slot_id, build_idx):
    templates, comp, slot, build = _find_build_or_404(comp_id, slot_id, build_idx)
    shared.set_build_equip(build, _parse_equip_form())
    if slot.get("builds"):
        slot["item_id"] = slot["builds"][0].get("item_id")
    shared.save_templates(templates)
    return redirect(url_for("comp_page", comp_id=comp_id))


@app.route("/comp/<comp_id>/slot/<slot_id>/build/<int:build_idx>/salvar_biblioteca", methods=["POST"])
def salvar_na_biblioteca(comp_id, slot_id, build_idx):
    templates, comp, slot, build = _find_build_or_404(comp_id, slot_id, build_idx)
    library = shared.load_build_library()
    novo_id = shared._slug(build["nome"])
    base_id, sufixo = novo_id, 2
    while any(b["id"] == novo_id for b in library):
        novo_id = f"{base_id}_{sufixo}"
        sufixo += 1
    novo = copy.deepcopy(build)
    novo["id"] = novo_id
    library.append(novo)
    shared.save_build_library(library)
    return redirect(url_for("build_equip_page", comp_id=comp_id, slot_id=slot_id, build_idx=build_idx))


# =========================================================================
# BIBLIOTECA DE BUILDS - loadouts salvos, reaproveitaveis em qualquer slot
# =========================================================================

BUILDS_LIB_TPL = """
<h1>Biblioteca de builds</h1>
<p class="subtitle">Monte uma build uma vez e reaproveite em qualquer slot de qualquer composicao. Na pagina de uma composicao, use "usar da biblioteca" pra copiar uma build daqui pra um slot. Marque uma build como <strong>publica</strong> pra ela aparecer na pagina <a href="{{ frontend_url }}/builds" target="_blank">Builds</a> do site, visivel pra qualquer jogador, com seu nome e avaliacao em estrelas.</p>

<div class="grid">
  {% for b in builds %}
  <div class="card" style="display:flex; flex-direction:column;">
    <a class="link" href="{{ url_for('lib_build_equip_page', build_id=b['id']) }}" style="color:inherit; text-decoration:none;">
      {% if b.get('item_id') %}
      <img src="{{ render_base }}/{{ b['item_id'] }}.png?count=1&quality=1" alt="" style="width:40px;height:40px;border-radius:8px;margin-bottom:8px;">
      {% endif %}
      <h3>{{ b['nome'] }}</h3>
      <div class="meta">{{ (b.get('equip') or {})|length }} peca(s) equipada(s)</div>
    </a>
    {% if b.get('visibility') == 'shared' %}
    <div class="meta" style="margin-top:8px; color:#e2bc62;">&#9733; Publica &middot; {{ b.get('categoria') or 'Sem categoria' }}</div>
    <form method="post" action="{{ url_for('despublicar_build_biblioteca', build_id=b['id']) }}" style="margin-top:8px;">
      <button class="btn secondary small" type="submit">Tornar privada</button>
    </form>
    {% else %}
    <form method="post" action="{{ url_for('compartilhar_build_biblioteca', build_id=b['id']) }}" style="margin-top:8px; display:flex; flex-direction:column; gap:6px;">
      <select name="papel" style="width:100%;">
        <option value="">Funcao na party (opcional)</option>
        {% for rid, rinfo in build_roles.items() %}
        <option value="{{ rid }}">{{ rinfo['label'] }}</option>
        {% endfor %}
      </select>
      <div style="display:flex; gap:6px;">
        <select name="categoria" style="flex:1; min-width:0;">
          {% for cat in build_categories %}
          <option value="{{ cat }}">{{ cat }}</option>
          {% endfor %}
        </select>
        <button class="btn small" type="submit">Compartilhar</button>
      </div>
    </form>
    {% endif %}
  </div>
  {% endfor %}

  <div class="card new-card">
    <form method="post" action="{{ url_for('nova_build_biblioteca') }}" class="field" style="width:100%">
      <label>Nova build</label>
      <div style="display:flex; gap:8px;">
        <input type="text" name="nome" placeholder="Ex: Alabarda ZvZ" required>
        <button class="btn" type="submit">+</button>
      </div>
    </form>
  </div>
</div>
{% if not builds %}
<div class="empty">Nenhuma build salva ainda. Crie uma acima, ou abra uma build dentro de uma composicao e clique em "Salvar essa build na biblioteca".</div>
{% endif %}
"""


@app.route("/builds")
def builds_lib_page():
    builds = shared.load_build_library()
    html = render_template_string(BUILDS_LIB_TPL, builds=builds, render_base=shared.RENDER_BASE, build_categories=BUILD_CATEGORIES, build_roles=BUILD_ROLES, frontend_url=FRONTEND_URL)
    return shell("Biblioteca de builds", html)


@app.route("/builds/novo", methods=["POST"])
def nova_build_biblioteca():
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return redirect(url_for("builds_lib_page"))
    library = shared.load_build_library()
    novo_id = shared._slug(nome)
    base_id, sufixo = novo_id, 2
    while any(b["id"] == novo_id for b in library):
        novo_id = f"{base_id}_{sufixo}"
        sufixo += 1
    library.append({"id": novo_id, "nome": nome, "item_id": None, "arma_nome": None, "emoji_id": None, "equip": {}})
    shared.save_build_library(library)
    return redirect(url_for("lib_build_equip_page", build_id=novo_id))


def _find_lib_build_or_404(build_id):
    library = shared.load_build_library()
    build = next((b for b in library if b["id"] == build_id), None)
    if not build:
        abort(404)
    return library, build


@app.route("/builds/<build_id>")
def lib_build_equip_page(build_id):
    library, build = _find_lib_build_or_404(build_id)
    crumb_items = [
        ("Conteudos", url_for("home")),
        ("Biblioteca de builds", url_for("builds_lib_page")),
        (build["nome"], None),
    ]
    save_url = url_for("salvar_lib_build_equip", build_id=build_id)
    excluir_url = url_for("excluir_build_biblioteca", build_id=build_id)
    renomear_url = url_for("renomear_build_biblioteca", build_id=build_id)
    extra = (
        f'<form method="post" action="{renomear_url}" class="inline" style="margin-top:14px; display:flex; gap:6px; align-items:center;">'
        f'<input type="text" name="nome" value="{build["nome"]}" required style="flex:1; min-width:0;">'
        f'<button class="btn secondary small" type="submit">Renomear</button></form>'
        f'<form method="post" action="{excluir_url}" class="inline" style="margin-top:10px;" '
        f'onsubmit="return confirm(\'Excluir essa build da biblioteca? Copias ja usadas em composicoes nao sao afetadas.\');">'
        f'<button class="btn danger small" type="submit">Excluir da biblioteca</button></form>'
    )
    return _render_equip_page(
        build, save_url, crumb_items, build["nome"],
        "Build salva na biblioteca · clique num quadrado pra montar o equipamento principal e os swaps.",
        extra, build["nome"] + " - Biblioteca",
    )


@app.route("/builds/<build_id>/salvar", methods=["POST"])
def salvar_lib_build_equip(build_id):
    library, build = _find_lib_build_or_404(build_id)
    shared.set_build_equip(build, _parse_equip_form())
    shared.save_build_library(library)
    return redirect(url_for("lib_build_equip_page", build_id=build_id))


@app.route("/builds/<build_id>/renomear", methods=["POST"])
def renomear_build_biblioteca(build_id):
    library, build = _find_lib_build_or_404(build_id)
    nome = (request.form.get("nome") or "").strip()
    if nome:
        build["nome"] = nome
        shared.save_build_library(library)
    return redirect(url_for("lib_build_equip_page", build_id=build_id))


@app.route("/builds/<build_id>/excluir", methods=["POST"])
def excluir_build_biblioteca(build_id):
    library = [b for b in shared.load_build_library() if b["id"] != build_id]
    shared.save_build_library(library)
    return redirect(url_for("builds_lib_page"))


@app.route("/builds/<build_id>/compartilhar", methods=["POST"])
def compartilhar_build_biblioteca(build_id):
    """Marca uma build da biblioteca como publica - passa a aparecer na
    pagina /builds-publicas (site inteiro, sem exigir login), com o nome de
    quem compartilhou (pego da sessao do painel legado) e a categoria
    escolhida no form."""
    library, build = _find_lib_build_or_404(build_id)
    ctx = _legacy_context()
    session = ctx[0] if ctx else None
    categoria = (request.form.get("categoria") or "").strip()[:40]
    if categoria not in BUILD_CATEGORIES:
        categoria = "Sem categoria"
    papel = (request.form.get("papel") or "").strip()
    if papel not in BUILD_ROLES:
        papel = None
    build["visibility"] = "shared"
    build["categoria"] = categoria
    build["papel"] = papel
    build["shared_by_name"] = (session.get("global_name") or session.get("username")) if session else None
    build["shared_by_name"] = build["shared_by_name"] or "Anonimo"
    build["shared_by_discord_id"] = session.get("discord_id") if session else None
    build["shared_workspace_id"] = shared.get_current_guild()
    build["shared_at"] = int(time.time())
    shared.save_build_library(library)
    return redirect(url_for("lib_build_equip_page", build_id=build_id))


@app.route("/builds/<build_id>/despublicar", methods=["POST"])
def despublicar_build_biblioteca(build_id):
    """Volta uma build compartilhada pra privada - some da pagina publica na
    hora (o rating fica guardado, caso ela seja compartilhada de novo)."""
    library, build = _find_lib_build_or_404(build_id)
    build["visibility"] = "private"
    shared.save_build_library(library)
    return redirect(url_for("lib_build_equip_page", build_id=build_id))


# =========================================================================
# ROSTER SIMPLES (classes.json - usado pelo /ping-funcao e como fallback de evento)
# =========================================================================

CLASSES_TPL = """
<div class="crumb"><a href="{{ url_for('home') }}">Conteudos</a> / Roster simples</div>
<h1>Roster simples</h1>
<p class="subtitle">Usado pelo comando /ping-funcao e como opcao padrao de /evento quando nenhuma composicao e escolhida. Cada classe = 1 build (sem swap). Pra builds com swap, use os Conteudos/Composicoes.</p>

{{ error_banner|safe }}

<div class="grid">
  {% for c in classes %}
  <div class="card">
    <div class="builds-row" style="margin-top:0; margin-bottom:10px;">{{ render_icon(c)|safe }}</div>
    <h3>{{ c['nome'] }}</h3>
    <div class="meta">Cargo: {{ c['cargo'] }} &middot; Vagas: {{ c.get('vagas') or 'sem limite' }}</div>
    <div class="card-actions">
      <a class="btn secondary small" href="{{ url_for('editar_classe_page', classe_id=c['id']) }}">Editar</a>
      <form method="post" action="{{ url_for('excluir_classe', classe_id=c['id']) }}" onsubmit="return confirm('Remover essa classe do roster simples?');">
        <button class="btn danger small" type="submit">Remover</button>
      </form>
    </div>
  </div>
  {% endfor %}
</div>

<div class="box">
  <h3>+ Adicionar classe</h3>
  <p class="hint">Uma classe do roster simples = uma unica build fixa (sem opcao de swap). Cada membro reage no /ping-funcao pra escolher a sua.</p>
  <form method="post" action="{{ url_for('nova_classe') }}">
    <div class="row3">
      <div class="field">
        <label>Nome {{ tip_nome_classe|safe }}</label>
        <input type="text" name="nome" id="novaclasse-nome" placeholder="Ex: Healer Sagrado" required oninput="if(!window.__cargoClasseTocado) document.getElementById('novaclasse-cargo').value = this.value;">
      </div>
      <div class="field">
        <label>Cargo do Discord {{ tip_cargo_classe|safe }}</label>
        <input type="text" name="cargo" id="novaclasse-cargo" placeholder="Ex: Healer Sagrado" required oninput="window.__cargoClasseTocado = true;">
      </div>
      <div class="field">
        <label>Vagas {{ tip_vagas_classe|safe }}</label>
        <input type="number" name="vagas" min="1" placeholder="sem limite">
      </div>
    </div>
    <div class="section-label">Build da classe {{ tip_build_classe|safe }}</div>
    <div class="field">
      <label style="margin-bottom:8px;">Da biblioteca ({{ build_library|length }} disponiveis)</label>
      {{ lib_picker_html|safe }}
    </div>
    <div class="or-divider">ou crie uma nova</div>
    <div class="field">
      {{ picker_html|safe }}
      <div class="meta" style="margin-top:6px;">Escolha uma arma - ela vira uma build nova, ja salva direto na Biblioteca de builds pra reusar depois.</div>
    </div>
    <button class="btn" type="submit">Adicionar</button>
  </form>
</div>
"""

EDITAR_CLASSE_TPL = """
<div class="crumb"><a href="{{ url_for('home') }}">Conteudos</a> / <a href="{{ url_for('classes_page') }}">Roster simples</a> / Editar</div>
<h1>Editar {{ classe['nome'] }}</h1>

{{ error_banner|safe }}

<div class="box">
  <form method="post" action="{{ url_for('editar_classe', classe_id=classe['id']) }}">
    <div class="row3">
      <div class="field">
        <label>Nome {{ tip_nome_classe|safe }}</label>
        <input type="text" name="nome" value="{{ classe['nome'] }}" required>
      </div>
      <div class="field">
        <label>Cargo do Discord {{ tip_cargo_classe|safe }}</label>
        <input type="text" name="cargo" value="{{ classe['cargo'] }}" required>
      </div>
      <div class="field">
        <label>Vagas {{ tip_vagas_classe|safe }}</label>
        <input type="number" name="vagas" min="1" value="{{ classe.get('vagas') or '' }}" placeholder="sem limite">
      </div>
    </div>
    <div class="section-label">Build da classe {{ tip_build_classe|safe }}</div>
    <div class="field">
      <label style="margin-bottom:8px;">Da biblioteca ({{ build_library|length }} disponiveis)</label>
      {{ lib_picker_html|safe }}
    </div>
    <div class="or-divider">ou troque por uma nova</div>
    <div class="field">
      {{ picker_html|safe }}
      <div class="meta" style="margin-top:6px;">Trocar a arma aqui atualiza a build da classe e ja salva a nova arma na Biblioteca de builds.</div>
    </div>
    <div class="card-actions">
      <button class="btn" type="submit">Salvar</button>
      <a class="btn secondary" href="{{ url_for('classes_page') }}">Cancelar</a>
    </div>
  </form>
</div>
"""


TIP_BUILD_CLASSE = tip("Escolha uma build ja salva na biblioteca (clique nela), ou digite uma arma nova logo abaixo - ela entra automaticamente na Biblioteca de builds e fica pronta pra reusar em qualquer classe ou composicao depois.")


@app.route("/classes")
def classes_page():
    classes = shared.load_classes()
    library = shared.load_build_library()
    picker_html = render_weapon_picker("novaclasse", "arma", False, [], "Arma")
    lib_picker_html = render_lib_picker_single("novaclasse", library, None)
    html = render_template_string(
        CLASSES_TPL, classes=classes, render_icon=render_icon, picker_html=picker_html,
        build_library=library, lib_picker_html=lib_picker_html,
        error_banner=error_banner(),
        tip_nome_classe=tip("Nome que aparece no card da classe (ex: Healer Sagrado, Tank Alabarda)."),
        tip_cargo_classe=tip("Nome do cargo (role) do Discord ligado a essa classe. Se a guilda nao usa cargos separados, so repita o nome."),
        tip_vagas_classe=tip("Quantas pessoas podem escolher essa classe ao mesmo tempo no /ping-funcao. Deixe em branco pra nao ter limite."),
        tip_build_classe=TIP_BUILD_CLASSE,
    )
    return shell("Roster simples", html)


@app.route("/classes/nova", methods=["POST"])
def nova_classe():
    nome = (request.form.get("nome") or "").strip()
    cargo = (request.form.get("cargo") or "").strip()
    vagas_raw = (request.form.get("vagas") or "").strip()
    arma_raw = (request.form.get("arma") or "").strip()
    build_lib_id = (request.form.get("build_lib") or "").strip()
    if not nome or not cargo:
        return redirect(url_for("classes_page", erro="campos_obrigatorios"))
    if not arma_raw and not build_lib_id:
        return redirect(url_for("classes_page", erro="sem_build"))

    classes = shared.load_classes()
    if len(classes) >= 25:
        return redirect(url_for("classes_page", erro="limite_classes"))

    item_id, arma_nome = None, None
    if build_lib_id:
        lib_build = next((b for b in shared.load_build_library() if b["id"] == build_lib_id), None)
        if lib_build:
            item_id, arma_nome = lib_build.get("item_id"), lib_build.get("arma_nome") or lib_build.get("nome")
    if not item_id and arma_raw:
        item_id, arma_nome = shared._parse_single_build_field(arma_raw)
        if not item_id and arma_nome:
            item_id = shared.resolve_item_id_by_name(arma_nome)
        _sync_weapon_to_library(item_id, arma_nome)

    classes.append({
        "id": shared._slug(nome), "nome": nome, "item_id": item_id, "arma_nome": arma_nome,
        "cargo": cargo, "vagas": int(vagas_raw) if vagas_raw.isdigit() else None,
        "ordem": len(classes) + 1, "role_id": None, "emoji_id": None,
    })
    shared.save_classes(classes)
    return redirect(url_for("classes_page"))


@app.route("/classes/<classe_id>/editar")
def editar_classe_page(classe_id):
    classes = shared.load_classes()
    classe = next((c for c in classes if c["id"] == classe_id), None)
    if not classe:
        abort(404)
    library = shared.load_build_library()
    selected_id = next((b["id"] for b in library if classe.get("item_id") and b.get("item_id") == classe["item_id"]), None)
    initial = [] if selected_id else ([(classe["item_id"], classe.get("arma_nome") or classe["item_id"])] if classe.get("item_id") else [])
    picker_html = render_weapon_picker("editclasse", "arma", False, initial, "Arma")
    lib_picker_html = render_lib_picker_single("editclasse", library, selected_id)
    html = render_template_string(
        EDITAR_CLASSE_TPL, classe=classe, picker_html=picker_html,
        build_library=library, lib_picker_html=lib_picker_html,
        error_banner=error_banner(),
        tip_nome_classe=tip("Nome que aparece no card da classe (ex: Healer Sagrado, Tank Alabarda)."),
        tip_cargo_classe=tip("Nome do cargo (role) do Discord ligado a essa classe. Se a guilda nao usa cargos separados, so repita o nome."),
        tip_vagas_classe=tip("Quantas pessoas podem escolher essa classe ao mesmo tempo no /ping-funcao. Deixe em branco pra nao ter limite."),
        tip_build_classe=TIP_BUILD_CLASSE,
    )
    return shell("Editar " + classe["nome"], html)


@app.route("/classes/<classe_id>/editar", methods=["POST"])
def editar_classe(classe_id):
    classes = shared.load_classes()
    classe = next((c for c in classes if c["id"] == classe_id), None)
    if not classe:
        abort(404)
    nome = (request.form.get("nome") or "").strip()
    cargo = (request.form.get("cargo") or "").strip()
    vagas_raw = (request.form.get("vagas") or "").strip()
    arma_raw = (request.form.get("arma") or "").strip()
    build_lib_id = (request.form.get("build_lib") or "").strip()
    if not nome or not cargo:
        return redirect(url_for("editar_classe_page", classe_id=classe_id, erro="campos_obrigatorios"))
    if nome:
        classe["nome"] = nome
    if cargo:
        classe["cargo"] = cargo
    classe["vagas"] = int(vagas_raw) if vagas_raw.isdigit() else None

    item_id, arma_nome = None, None
    if build_lib_id:
        lib_build = next((b for b in shared.load_build_library() if b["id"] == build_lib_id), None)
        if lib_build:
            item_id, arma_nome = lib_build.get("item_id"), lib_build.get("arma_nome") or lib_build.get("nome")
    elif arma_raw:
        item_id, arma_nome = shared._parse_single_build_field(arma_raw)
        if not item_id and arma_nome:
            item_id = shared.resolve_item_id_by_name(arma_nome)
        _sync_weapon_to_library(item_id, arma_nome)
    if item_id:
        classe["item_id"] = item_id
        classe["arma_nome"] = arma_nome

    shared.save_classes(classes)
    return redirect(url_for("classes_page"))


@app.route("/classes/<classe_id>/excluir", methods=["POST"])
def excluir_classe(classe_id):
    classes = [c for c in shared.load_classes() if c["id"] != classe_id]
    for i, c in enumerate(classes, start=1):
        c["ordem"] = i
    shared.save_classes(classes)
    return redirect(url_for("classes_page"))


# =========================================================================
# AUTENTICACAO (Discord OAuth2) - login real do dashboard
# =========================================================================
# Fluxo: /auth/discord/login redireciona pro Discord -> usuario aprova ->
# Discord manda de volta pra /auth/discord/callback com um "code" -> trocamos
# esse code por um token de acesso, buscamos quem e o usuario e em quais
# guildas ele e admin (escopo "identify guilds"), cruzamos com as guildas que
# JA TEM esse bot instalado (pasta em guild_data/) e, se bater pelo menos uma,
# criamos uma sessao (token opaco, sem cookie - o front e outro dominio) e
# mandamos o usuario de volta pro site com esse token.
#
# Isso so identifica QUEM e o usuario e QUAIS guildas ele administra. Nao
# checa pagamento/assinatura ainda (fase separada, precisa do Stripe
# configurado primeiro) - por enquanto, "acesso liberado" = "e admin de uma
# guilda que ja tem o bot instalado".

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "https://147-15-80-15.sslip.io/auth/discord/callback")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://seagreen-rat-589164.hostingersite.com")

SESSIONS_FILE = shared.BASE_DIR / "auth_sessions.json"
PERM_ADMINISTRATOR = 0x8
PERM_MANAGE_GUILD = 0x20
SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 14  # 14 dias

# --- Stripe (checkout de assinatura dos slots) ---------------------------
# Chamado direto via REST (sem o pacote `stripe`) - o backend ja tem
# `requests` importado pro OAuth do Discord, entao evita mais uma dependencia
# no venv da VM so pra isso. Verificacao de assinatura do webhook e feita a
# mao (HMAC-SHA256), e o algoritmo documentado pela Stripe.
STRIPE_API_BASE = "https://api.stripe.com/v1"
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_GUILD = os.environ.get("STRIPE_PRICE_GUILD", "price_1U4gZf8dRbvUJ0FlqkP17Qcl")
STRIPE_PRICE_PERSONAL = os.environ.get("STRIPE_PRICE_PERSONAL", "price_1U4gZh8dRbvUJ0Fl5ckU37br")
STRIPE_PRICE_GUILD_ADDITIONAL = os.environ.get("STRIPE_PRICE_GUILD_ADDITIONAL", "price_1U4hed8dRbvUJ0Flw3W3ukva")
STRIPE_PRICE_PERSONAL_ADDITIONAL = os.environ.get("STRIPE_PRICE_PERSONAL_ADDITIONAL", "price_1U4hee8dRbvUJ0FlaCBLzli9")
# Cupom "primeiro mes com desconto" (R$29,90 -> R$19,90) - so no slot de
# guilda, aplicado uma vez (duration=once na Stripe) na primeira fatura da
# assinatura; das renovacoes em diante cobra o preco cheio.
STRIPE_COUPON_GUILD_FIRST_MONTH = os.environ.get("STRIPE_COUPON_GUILD_FIRST_MONTH", "M2ZB4K9b")

# Valor de cada preco em centavos (BRL) - so usado pro mandate do Pix
# Automatico (ele exige o valor exato da cobranca recorrente, a Stripe nao
# descobre isso sozinha a partir do price_id). Confirmado direto na Stripe
# em 2026-08-17; se o preco mudar no Dashboard, atualiza aqui tambem (ou via
# env var, sem precisar mexer no codigo).
STRIPE_AMOUNT_GUILD = int(os.environ.get("STRIPE_AMOUNT_GUILD", "2990"))
STRIPE_AMOUNT_PERSONAL = int(os.environ.get("STRIPE_AMOUNT_PERSONAL", "1490"))
STRIPE_AMOUNT_GUILD_ADDITIONAL = int(os.environ.get("STRIPE_AMOUNT_GUILD_ADDITIONAL", "1590"))
STRIPE_AMOUNT_PERSONAL_ADDITIONAL = int(os.environ.get("STRIPE_AMOUNT_PERSONAL_ADDITIONAL", "1000"))


def _stripe_post(path, data):
    """POST form-encoded na API da Stripe (Basic Auth com a secret key como
    usuario, senha vazia - e assim que a Stripe autentica chamadas server-
    side). `data` aceita chaves com colchete (ex: 'metadata[discord_id]')
    pra parametros aninhados, igual o formato que a API da Stripe espera."""
    resp = requests.post(
        f"{STRIPE_API_BASE}/{path}",
        auth=(STRIPE_SECRET_KEY, ""),
        data=data,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _stripe_get(path, params=None):
    resp = requests.get(
        f"{STRIPE_API_BASE}/{path}",
        auth=(STRIPE_SECRET_KEY, ""),
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _pix_mandate_fields(amount_centavos):
    """Habilita Pix Automatico (mandato de cobranca recorrente) nesse
    checkout, alem do cartao. So funciona de verdade depois que a conta
    Stripe tiver Pix habilitado nos metodos de pagamento (settings) - ate
    la a Stripe simplesmente nao oferece a opcao Pix na tela de checkout,
    sem quebrar o fluxo de cartao."""
    return {
        "payment_method_options[pix][mandate_options][payment_schedule]": "monthly",
        "payment_method_options[pix][mandate_options][amount]": amount_centavos,
    }


def _stripe_customer_field(discord_id):
    """Se esse discord_id ja tiver virado Customer na Stripe (de qualquer
    slot anterior), reaproveita o mesmo customer no proximo checkout - assim
    o Customer Portal mostra todas as assinaturas da pessoa num lugar so, em
    vez de espalhar em customers diferentes a cada compra."""
    customer_id = shared.stripe_customer_id_for_user(discord_id)
    return {"customer": customer_id} if customer_id else {}


def _verify_stripe_signature(payload: bytes, sig_header: str, secret: str, tolerance: int = 300) -> bool:
    """Confere a assinatura HMAC-SHA256 do header Stripe-Signature (formato
    't=<timestamp>,v1=<hash>') contra o corpo bruto da requisicao - e assim
    que a Stripe garante que o webhook e mesmo dela e nao foi adulterado.
    Sem isso qualquer um poderia forjar um POST em /webhooks/stripe e criar
    slot de graca."""
    try:
        parts = dict(kv.split("=", 1) for kv in sig_header.split(","))
        timestamp = parts["t"]
        v1 = parts["v1"]
    except (KeyError, ValueError):
        return False
    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        return False
    try:
        if abs(time.time() - int(timestamp)) > tolerance:
            return False
    except ValueError:
        return False
    return True


def _load_sessions():
    return shared._load_json(SESSIONS_FILE, {})


def _save_sessions(data):
    shared._save_json(SESSIONS_FILE, data)


def _tenant_guild_ids():
    """IDs de guilda que ja tem esse bot instalado (tem pasta propria em
    guild_data/) - e a lista de "clientes" existentes por enquanto."""
    if not shared.GUILD_DATA_DIR.exists():
        return set()
    return {p.name for p in shared.GUILD_DATA_DIR.iterdir() if p.is_dir()}


def _current_session():
    """Le o token Bearer do header Authorization e devolve os dados da sessao,
    ou None se nao tiver token, for invalido ou tiver expirado."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token:
        return None
    sessions = _load_sessions()
    s = sessions.get(token)
    if not s or s.get("expires_at", 0) < time.time():
        return None
    return s


def require_session():
    """Helper pras rotas que exigem login - devolve a sessao ou aborta 401."""
    s = _current_session()
    if not s:
        abort(401)
    return s


def _usable_workspaces(session):
    """Workspaces (guilda ou pessoal) que esse usuario pode USAR agora - tem
    slot ATIVO comprado pra eles. Fonte da verdade e slots.json, nao a
    sessao - assim uma compra de slot vale na hora, sem precisar deslogar/
    logar de novo. Guilda: 1 slot tipo 'guild' = acesso aquele guild_id.
    Pessoal: 1 slot tipo 'personal' = 1 espaco pessoal usavel (com o proprio
    workspace_id e label - da pra ter varios)."""
    discord_id = session.get("discord_id")
    eligible_by_id = {g["id"]: g for g in session.get("eligible_guilds", [])}
    usable = []
    for slot in shared.slots_for_user(discord_id):
        if slot.get("type") == "guild":
            gid = slot.get("guild_id")
            if not gid:
                continue
            info = eligible_by_id.get(gid, {})
            usable.append({
                "id": gid,
                "name": info.get("name") or f"Guilda {gid}",
                "icon": info.get("icon"),
                "personal": False,
                "slot_id": slot.get("id"),
            })
        elif slot.get("type") == "personal":
            wid = slot.get("workspace_id")
            if not wid:
                continue
            usable.append({
                "id": wid,
                "name": slot.get("label") or "Uso pessoal",
                "icon": None,
                "personal": True,
                "slot_id": slot.get("id"),
            })
    # Sem assinatura: toda guilda que essa pessoa administra no Discord e que
    # ja tem o bot instalado entra direto, mesmo sem slot comprado.
    ja = {w["id"] for w in usable}
    for gid, info in eligible_by_id.items():
        if gid in ja:
            continue
        usable.append({
            "id": gid,
            "name": info.get("name") or f"Guilda {gid}",
            "icon": info.get("icon"),
            "personal": False,
            "slot_id": None,
            "nivel": info.get("nivel") or "admin",
        })
    return usable


def _locked_guilds(session):
    """Guildas que esse usuario administra no Discord (bot ja instalado) mas
    ainda NAO tem slot comprado - aparecem travadas no seletor do dashboard,
    com botao de comprar. So existe pra guilda (pessoal nao trava por
    'elegibilidade' - qualquer um pode comprar mais um espaco pessoal)."""
    unlocked_ids = {
        s.get("guild_id") for s in shared.slots_for_user(session.get("discord_id"))
        if s.get("type") == "guild"
    }
    return [g for g in session.get("eligible_guilds", []) if g["id"] not in unlocked_ids]


def _resolve_guild_id(session, source):
    """Resolve e valida qual workspace (guilda ou pessoal) uma chamada
    /api/* e sobre: le 'guild_id' de `source` (request.args), confere que
    esse usuario TEM SLOT ativo pra ele, e cai pro primeiro usavel se nao
    vier nenhum (cobre o caso comum de so ter 1 workspace - nao obriga o
    front a sempre mandar o param)."""
    usable_ids = [w["id"] for w in _usable_workspaces(session)]
    requested = source.get("guild_id")
    if requested:
        if requested not in usable_ids:
            abort(403)
        return requested
    if usable_ids:
        return usable_ids[0]
    abort(403)


def require_guild_session():
    """Pras rotas /api/* que leem/escrevem dado de um workspace especifico -
    exige sessao Bearer valida, resolve+valida o guild_id (?guild_id= na
    query string, contra os workspaces com slot ativo) e ja deixa marcado no
    contexto ambiente (shared.set_current_guild) - o resto da rota chama
    load_/save_* normal."""
    session = require_session()
    guild_id = _resolve_guild_id(session, request.args)
    shared.set_current_guild(guild_id)
    return session, guild_id


# --- painel avancado (legado, HTML server-side) -------------------------
# Essas paginas sao navegadas direto no browser (nao chamadas por fetch/JS),
# entao nao da pra usar o Bearer token do dashboard novo direto - a ponte e a
# rota /legacy/enter, que o dashboard linka ja com o token (do localStorage)
# e o guild_id ativo. Dali em diante um cookie carrega a sessao sozinho em
# toda navegacao dentro do painel (mesma origem), sem precisar tocar em
# nenhum link/formulario das paginas legadas.
LEGACY_TOKEN_COOKIE = "legacy_token"
LEGACY_GUILD_COOKIE = "legacy_guild"
LEGACY_COOKIE_MAX_AGE = 60 * 60 * 12  # 12h - painel de uso pontual, sessao curta


def _legacy_context():
    """Sessao+guilda do painel legado via cookie, ou None se invalido/sem
    cookie/guilda que esse usuario nao tem mais slot ativo pra ela."""
    token = request.cookies.get(LEGACY_TOKEN_COOKIE)
    guild_id = request.cookies.get(LEGACY_GUILD_COOKIE)
    if not token or not guild_id:
        return None
    sessions = _load_sessions()
    session = sessions.get(token)
    if not session or session.get("expires_at", 0) < time.time():
        return None
    usable_ids = {w["id"] for w in _usable_workspaces(session)}
    if guild_id not in usable_ids:
        return None
    return session, guild_id



# =========================================================================
# PAINEL DE PINGS (legado, HTML server-side) - criar/editar/encerrar pings
# de conteudo e dividir o loot direto pelo site, sem precisar dos comandos
# do Discord. Acoes que precisam da conexao viva com o Discord (postar a
# mensagem, criar o topico, editar o embed) passam pela API interna do bot
# (_internal_call) - ver "API INTERNA" no event_bot.py.
# =========================================================================

COMANDOS_BOT = [
    ("Conteudos (pings)", [
        ("/ping-conteudo", "Cria um ping de recrutamento num canal, com topico, imagem da build e vagas por botao (por funcao). Pede uma composicao ja cadastrada como referencia."),
        ("/encerrar-ping", "Encerra um ping aberto (trava as vagas, publica o roster final) e pergunta se vai ter divisao de loot."),
        ("/ping-funcao", "Publica o painel fixo de funcoes do roster simples (classes.json) - clique pra ser marcado e notificado quando alguem chamar aquela funcao."),
    ]),
    ("Evento (call com tempo de presenca)", [
        ("/evento iniciar", "Inicia um evento agora: cria a call de voz e o painel de classes/composicao pra galera entrar."),
        ("/evento agendar", "Agenda um evento pro futuro - a call e o painel sao criados sozinhos na hora marcada."),
        ("/evento agenda", "Mostra os proximos eventos agendados."),
        ("/evento status", "Mostra o andamento (tempo de presenca de cada um) de um evento em aberto."),
        ("/evento finalizar", "Encerra o evento, calcula o tempo de presenca de cada participante e inicia a divisao de loot."),
        ("/evento cancelar", "Cancela um evento sem dividir loot (ex: criado por engano)."),
    ]),
    ("Saldo (loot guardado)", [
        ("/saldo membro", "Mostra o seu proprio saldo acumulado de loot saldado."),
        ("/saldo todos", "[gestor de saldo] Mostra o saldo total (bruto) e o saldo de cada membro da guilda."),
        ("/saldo-consultar", "[gestor de saldo] Mostra o saldo acumulado de um membro especifico."),
        ("/saldo-quitar", "[gestor de saldo] Da baixa (zera ou desconta um valor) no saldo de um membro - marca como pago fora do bot."),
        ("/saldo-gestor", "[contratante/dono do slot] Adiciona ou remove alguem como gestor de saldo dessa guilda."),
    ]),
    ("Classes e composicoes", [
        ("/classes listar", "Lista as classes configuradas no roster simples."),
        ("/classes adicionar", "Adiciona uma classe ao roster (maximo 25 no total)."),
        ("/classes remover", "Remove uma classe do roster pelo nome."),
        ("/classes importar", "Importa o roster inteiro a partir da planilha classes_evento_modelo.xlsx preenchida."),
        ("/classes painel", "Abre um painel interativo pra adicionar/editar/remover classes sem decorar o comando."),
        ("/comp listar", "Lista as composicoes (templates) cadastradas e os slots de cada uma."),
        ("/comp criar", "Cria uma composicao vazia (depois adiciona slots com /comp slot_adicionar)."),
        ("/comp remover", "Remove uma composicao inteira."),
        ("/comp slot_adicionar", "Adiciona um slot a uma composicao (aceita 1 ou mais builds, separadas por virgula)."),
        ("/comp slot_remover", "Remove um slot de uma composicao."),
        ("/comp painel", "Painel interativo pra criar/editar composicoes e seus slots."),
    ]),
    ("Reputacao e outros", [
        ("/perfil", "Mostra a reputacao de um jogador (avaliacoes recebidas como caller e como player)."),
        ("/sync", "[admin do servidor] Forca a sincronizacao instantanea dos comandos slash nesse servidor (sem isso pode levar ate 1h pra aparecer)."),
    ]),
]

PINGS_TPL = """
<div class="crumb"><a href="{{ url_for('home') }}">Conteudos</a> / Painel de pings</div>
<h1>Painel de pings</h1>
<p class="subtitle">Crie, edite e encerre pings de conteudo direto do site, com divisao de loot guiada - sem precisar abrir o Discord. As mensagens continuam sendo postadas normalmente no canal/topico escolhido.</p>

{{ error_banner|safe }}
{% if ok_msg %}<div class="banner ok">&#10003; {{ ok_msg }}</div>{% endif %}

<div class="flex-between" style="margin-bottom:18px">
  <div class="tag-select">
    <a class="btn small{{ ' secondary' if tab != 'abertos' else '' }}" href="{{ url_for('pings_painel', tab='abertos') }}">Abertos ({{ abertos|length }})</a>
    <a class="btn small{{ ' secondary' if tab != 'encerrados' else '' }}" href="{{ url_for('pings_painel', tab='encerrados') }}">Encerrados ({{ encerrados|length }})</a>
    <a class="btn small{{ ' secondary' if tab != 'comandos' else '' }}" href="{{ url_for('pings_painel', tab='comandos') }}">Comandos do bot</a>
  </div>
  <a class="btn" href="{{ url_for('pings_painel_novo') }}">+ Novo ping</a>
</div>

{% if tab == 'comandos' %}
  <div class="box">
    <h3>Todos os comandos do bot</h3>
    <p class="hint">Slash commands disponiveis no Discord (digite <code>/</code> no servidor pra ver a lista com autocomplete). Alguns exigem permissao de gerenciar cargos, ser o gestor de saldo, ou ser o contratante (dono do slot).</p>
    {% for grupo, comandos in comandos_bot %}
      <h3 style="margin-top:26px">{{ grupo }}</h3>
      <div class="grid" style="grid-template-columns: 1fr">
        {% for nome, desc in comandos %}
        <div class="card" style="display:flex; gap:14px; align-items:flex-start">
          <code style="color:#fff; font-weight:700; white-space:nowrap; background:var(--bg-soft); padding:4px 10px; border-radius:6px; font-size:13px">{{ nome }}</code>
          <span class="meta" style="margin-top:2px">{{ desc }}</span>
        </div>
        {% endfor %}
      </div>
    {% endfor %}
  </div>
{% else %}
  {% set lista = abertos if tab == 'abertos' else encerrados %}
  {% if not lista %}
    <div class="empty">{{ 'Nenhum ping aberto no momento.' if tab == 'abertos' else 'Nenhum ping encerrado ainda.' }}</div>
  {% else %}
    {% for p in lista %}
    <div class="slot-card">
      <div class="slot-head">
        <div>
          <h3><a href="{{ url_for('pings_painel_detalhe', key=p.key) }}">{{ p.titulo }}</a></h3>
          <div class="meta">
            {{ p.confirmados }}/{{ p.vagas_total }} confirmados
            {% if p.local %} &middot; \U0001F4CD {{ p.local }}{% endif %}
            {% if p.fee %} &middot; \U0001F4B0 {{ p.fee }}{% endif %}
            {% if p.quando_fmt %} &middot; ⏰ {{ p.quando_fmt }}{% endif %}
            &middot; criado por {{ p.criado_por_nome or '?' }}
          </div>
        </div>
        <div class="tag-select">
          {% if p.encerrado and p.loot_dividido %}<span class="pill">loot dividido</span>{% endif %}
          {% if p.encerrado and not p.loot_dividido %}<a class="btn small" href="{{ url_for('pings_painel_detalhe', key=p.key) }}">Dividir loot</a>{% endif %}
          <a class="btn small secondary" href="{{ url_for('pings_painel_detalhe', key=p.key) }}">Abrir</a>
        </div>
      </div>
    </div>
    {% endfor %}
  {% endif %}
{% endif %}
"""

PING_NOVO_TPL = """
<div class="crumb"><a href="{{ url_for('home') }}">Conteudos</a> / <a href="{{ url_for('pings_painel') }}">Painel de pings</a> / Novo ping</div>
<h1>Novo ping</h1>
<p class="subtitle">Escolha o canal do Discord onde o topico vai ser criado e a composicao de referencia (precisa ter pelo menos 1 slot com build).</p>

{{ error_banner|safe }}

<form method="POST" class="box">
  <div class="row2">
    <div class="field">
      <label>Canal do Discord</label>
      <select name="canal_id" required>
        <option value="">Selecione...</option>
        {% for c in canais %}<option value="{{ c.id }}">#{{ c.name }}{% if c.category %} ({{ c.category }}){% endif %}</option>{% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Composicao de referencia</label>
      <select name="composicao_id" required>
        <option value="">Selecione...</option>
        {% for t in composicoes %}<option value="{{ t.id }}">{{ t.nome }} ({{ t.slots_count }} slots)</option>{% endfor %}
      </select>
    </div>
  </div>
  <div class="field">
    <label>Titulo <span class="info-tip" data-tip="Deixe em branco pra usar o nome da composicao.">i</span></label>
    <input type="text" name="titulo" maxlength="80" placeholder="Ex: Gank Noturno">
  </div>
  <div class="row3">
    <div class="field">
      <label>Local</label>
      <input type="text" name="local" maxlength="60" placeholder="Ex: HO, Caminho da Fe">
    </div>
    <div class="field">
      <label>Fee</label>
      <input type="text" name="fee" maxlength="40" placeholder="Gratis ou ex: 5%" value="Gratis">
    </div>
    <div class="field">
      <label>Horario <span class="info-tip" data-tip="Formato DD/MM HH:MM. Deixe em branco pra 'ao vivo agora'.">i</span></label>
      <input type="text" name="horario" maxlength="20" placeholder="17/08 21:00">
    </div>
  </div>
  <div class="field">
    <label>Vagas (uma funcao por linha) <span class="info-tip" data-tip="Repita o nome da funcao em varias linhas pra somar vagas. Pra mostrar o loot de cada vaga, escreva: Main Tank | Loot: Mapas">i</span></label>
    <textarea name="vagas_texto" placeholder="Tank&#10;Tank&#10;Healer pt&#10;Suporte&#10;DPS&#10;DPS" required></textarea>
  </div>
  <button type="submit" class="btn">Criar ping</button>
  <a href="{{ url_for('pings_painel') }}" class="btn secondary">Cancelar</a>
</form>
<script>
(function () {
  const VAGAS = {{ composicoes|map(attribute='vagas_texto', default='')|list|tojson }};
  const IDS = {{ composicoes|map(attribute='id')|list|tojson }};
  const sel = document.querySelector('select[name="composicao_id"]');
  const area = document.querySelector('textarea[name="vagas_texto"]');
  let auto = true;
  area.addEventListener('input', function () { auto = false; });
  sel.addEventListener('change', function () {
    const i = IDS.indexOf(sel.value);
    if (i >= 0 && (auto || !area.value.trim())) { area.value = VAGAS[i] || ''; auto = true; }
  });
})();
</script>
"""

PING_DETALHE_TPL = """
<div class="crumb"><a href="{{ url_for('home') }}">Conteudos</a> / <a href="{{ url_for('pings_painel') }}">Painel de pings</a> / {{ p.titulo }}</div>
<h1>{{ p.titulo }}</h1>
<p class="subtitle">
  Criado por {{ p.criado_por_nome or '?' }}
  {% if p.encerrado %} &middot; <strong style="color:var(--danger)">Encerrado</strong>{% else %} &middot; <strong style="color:var(--ok)">Aberto</strong>{% endif %}
  {% if p.loot_dividido %} &middot; loot ja dividido ({{ '%.0f'|format(p.loot_liquido or 0) }} prata liquido){% endif %}
</p>

{{ error_banner|safe }}
{% if ok_msg %}<div class="banner ok">&#10003; {{ ok_msg }}</div>{% endif %}

<div class="box">
  <h3>Confirmados por funcao</h3>
  <div class="grid" style="grid-template-columns: 1fr">
    {% for f in p.funcoes %}
    <div class="card" style="display:flex; justify-content:space-between; align-items:center">
      <span>{{ f.emoji }} <strong>{{ f.nome }}</strong></span>
      <span class="meta">{{ f.confirmados }}/{{ f.vagas }} confirmados</span>
    </div>
    {% endfor %}
  </div>
</div>

{% if not p.encerrado %}
<div class="box">
  <h3>Editar ping</h3>
  <p class="hint">Da pra reduzir vagas ate o numero ja confirmado - nao da pra tirar vaga de quem ja confirmou por aqui.</p>
  <form method="POST" action="{{ url_for('pings_painel_editar', key=p.key) }}">
    <div class="field">
      <label>Titulo</label>
      <input type="text" name="titulo" maxlength="80" value="{{ p.titulo }}">
    </div>
    <div class="row3">
      <div class="field">
        <label>Local</label>
        <input type="text" name="local" maxlength="60" value="{{ p.local or '' }}">
      </div>
      <div class="field">
        <label>Fee</label>
        <input type="text" name="fee" maxlength="40" value="{{ p.fee or '' }}">
      </div>
      <div class="field">
        <label>Horario (DD/MM HH:MM)</label>
        <input type="text" name="horario" maxlength="20" value="{{ p.horario_input or '' }}">
      </div>
    </div>
    <div class="row2" style="grid-template-columns: repeat(auto-fill, minmax(160px,1fr))">
      {% for f in p.funcoes %}
      <div class="field">
        <label>{{ f.emoji }} {{ f.nome }} (vagas)</label>
        <input type="number" name="vagas__{{ loop.index0 }}" min="{{ f.confirmados }}" value="{{ f.vagas }}">
        <input type="hidden" name="vaganome__{{ loop.index0 }}" value="{{ f.nome }}">
      </div>
      {% endfor %}
    </div>
    <button type="submit" class="btn">Salvar alteracoes</button>
  </form>

  <form method="POST" action="{{ url_for('pings_painel_encerrar', key=p.key) }}" style="margin-top:20px" onsubmit="return confirm('Encerrar esse ping? As vagas ficam travadas e ninguem mais consegue confirmar.')">
    <button type="submit" class="btn danger">Encerrar ping</button>
  </form>
</div>
{% elif not p.loot_dividido %}
<div class="box">
  <h3>Dividir loot</h3>
  <p class="hint">Divide entre todo mundo que confirmou presenca em alguma funcao (igual pra cada um). "Saldar" credita o valor no saldo acumulado de cada um, sem precisar pagar na hora.</p>
  <form method="POST" action="{{ url_for('pings_painel_dividir', key=p.key) }}">
    <div class="field">
      <label>O que fazer com o loot?</label>
      <select name="modo" id="modo-select" onchange="var mostra = this.value === 'comprador' || this.value === 'saldar'; document.getElementById('reparo-field').style.display = mostra ? 'block' : 'none'; document.getElementById('taxa-field').style.display = mostra ? 'block' : 'none';">
        <option value="dividir">Dividir tudo entre os confirmados (mostra o valor de cada um)</option>
        <option value="comprador">Alguem vai comprar o loot (desconta a taxa de venda + reparo)</option>
        <option value="saldar">Saldar (creditar no saldo acumulado de cada um)</option>
      </select>
    </div>
    <div class="row2">
      <div class="field">
        <label>Valor total do profit (prata)</label>
        <input type="text" name="profit" placeholder="Ex: 5000000" required>
      </div>
      <div class="field" id="reparo-field" style="display:none">
        <label>Valor do reparo (prata)</label>
        <input type="text" name="reparo" placeholder="Ex: 300000 (ou 0 se nao teve)">
      </div>
      <div class="field" id="taxa-field" style="display:none">
        <label>Taxa de venda (%)</label>
        <input type="text" name="taxa_pct" placeholder="Ex: 15 (ou 0 se nao teve)" value="15">
      </div>
    </div>
    <p class="hint">No "Saldar", reparo e taxa so sao descontados se voce preencher algo diferente de 0 - zere os dois campos pra creditar o valor cheio (igual antes).</p>
    <button type="submit" class="btn">Confirmar divisao</button>
  </form>
</div>
{% endif %}
"""


def _fmt_quando(ts):
    if not ts:
        return None
    try:
        from datetime import datetime as _dt
        return _dt.fromtimestamp(float(ts), shared.TIMEZONE).strftime("%d/%m %H:%M")
    except Exception:
        return None


def _fmt_horario_input(ts):
    if not ts:
        return ""
    try:
        from datetime import datetime as _dt
        return _dt.fromtimestamp(float(ts), shared.TIMEZONE).strftime("%d/%m %H:%M")
    except Exception:
        return ""


@app.route("/pings-painel")
def pings_painel():
    guild_id = shared.get_current_guild()
    tab = request.args.get("tab", "abertos")
    if tab not in ("abertos", "encerrados", "comandos"):
        tab = "abertos"
    pings = shared.load_pings(guild_id)
    itens = sorted(pings.items(), key=lambda kv: kv[1].get("criado_em", 0), reverse=True)

    def _row(key, p):
        confirmados = sum(len(f.get("claimed", [])) + len(f.get("extra", [])) for f in p.get("funcoes", []))
        vagas_total = sum(f.get("vagas", 0) for f in p.get("funcoes", []))
        return {
            "key": key, "titulo": p.get("titulo"), "local": p.get("local"), "fee": p.get("fee"),
            "quando_fmt": _fmt_quando(p.get("quando")), "criado_por_nome": p.get("criado_por_nome"),
            "confirmados": confirmados, "vagas_total": vagas_total,
            "encerrado": bool(p.get("encerrado")), "loot_dividido": bool(p.get("loot_dividido")),
        }

    abertos = [_row(k, p) for k, p in itens if not p.get("encerrado")]
    encerrados = [_row(k, p) for k, p in itens if p.get("encerrado")]

    html = render_template_string(
        PINGS_TPL, tab=tab, abertos=abertos, encerrados=encerrados,
        comandos_bot=COMANDOS_BOT, error_banner=error_banner(), ok_msg=request.args.get("ok"),
    )
    return shell("Painel de pings", html)


@app.route("/pings-painel/novo", methods=["GET", "POST"])
def pings_painel_novo():
    guild_id = shared.get_current_guild()

    if request.method == "POST":
        ctx = _legacy_context()
        session = ctx[0] if ctx else None
        data, status = _internal_call(
            "POST", "/pings", guild_id=guild_id,
            canal_id=request.form.get("canal_id"),
            composicao_id=request.form.get("composicao_id"),
            titulo=request.form.get("titulo"),
            local=request.form.get("local"),
            fee=request.form.get("fee"),
            horario=request.form.get("horario"),
            vagas_texto=request.form.get("vagas_texto"),
            criado_por_discord_id=session.get("discord_id") if session else None,
            criado_por_nome=(session.get("global_name") or session.get("username")) if session else "Painel do site",
        )
        if status != 200:
            html = render_template_string(
                PING_NOVO_TPL,
                canais=_internal_call("GET", "/channels", guild_id=guild_id)[0].get("channels", []),
                composicoes=shared.load_templates(guild_id),
                error_banner=f'<div class="banner error">&#9888; {data.get("error", "Nao foi possivel criar o ping.")}</div>',
            )
            return shell("Novo ping", html)
        return redirect(url_for("pings_painel_detalhe", key=data["ping_id"], ok="Ping criado com sucesso."))

    canais_data, _ = _internal_call("GET", "/channels", guild_id=guild_id)
    templates = shared.load_templates(guild_id)
    composicoes = [{"id": t["id"], "nome": t["nome"], "slots_count": len(t.get("slots", [])),
                    "vagas_texto": shared.vagas_texto_da_comp(t)} for t in templates if t.get("slots")]
    html = render_template_string(
        PING_NOVO_TPL, canais=canais_data.get("channels", []), composicoes=composicoes, error_banner="",
    )
    return shell("Novo ping", html)


def _ping_detail_ctx(key):
    guild_id = shared.get_current_guild()
    pings = shared.load_pings(guild_id)
    p = pings.get(key)
    if not p:
        abort(404)
    return {
        "key": key, "titulo": p.get("titulo"), "local": p.get("local"), "fee": p.get("fee"),
        "horario_input": _fmt_horario_input(p.get("quando")),
        "criado_por_nome": p.get("criado_por_nome"), "encerrado": bool(p.get("encerrado")),
        "loot_dividido": bool(p.get("loot_dividido")), "loot_liquido": p.get("loot_liquido"),
        "funcoes": [
            {"nome": f.get("nome"), "emoji": f.get("emoji"), "vagas": f.get("vagas"), "confirmados": len(f.get("claimed", []))}
            for f in p.get("funcoes", [])
        ],
    }


@app.route("/pings-painel/<key>")
def pings_painel_detalhe(key):
    p = _ping_detail_ctx(key)
    msg = request.args.get("msg")
    banner = f'<div class="banner error">&#9888; {msg}</div>' if msg else error_banner()
    html = render_template_string(PING_DETALHE_TPL, p=p, error_banner=banner, ok_msg=request.args.get("ok"))
    return shell(p["titulo"], html)


@app.route("/pings-painel/<key>/editar", methods=["POST"])
def pings_painel_editar(key):
    guild_id = shared.get_current_guild()
    vagas = []
    i = 0
    while f"vaganome__{i}" in request.form:
        nome = request.form.get(f"vaganome__{i}")
        vagas_raw = request.form.get(f"vagas__{i}")
        if nome and vagas_raw is not None and vagas_raw.strip() != "":
            vagas.append({"nome": nome, "vagas": vagas_raw})
        i += 1

    data, status = _internal_call(
        "PUT", f"/pings/{key}", guild_id=guild_id,
        titulo=request.form.get("titulo"), local=request.form.get("local"),
        fee=request.form.get("fee"), horario=request.form.get("horario"), vagas=vagas,
    )
    if status != 200:
        return redirect(url_for("pings_painel_detalhe", key=key, msg=data.get("error") or "Nao foi possivel salvar."))
    return redirect(url_for("pings_painel_detalhe", key=key, ok="Alteracoes salvas."))


@app.route("/pings-painel/<key>/encerrar", methods=["POST"])
def pings_painel_encerrar(key):
    guild_id = shared.get_current_guild()
    data, status = _internal_call("POST", f"/pings/{key}/encerrar", guild_id=guild_id)
    if status != 200:
        return redirect(url_for("pings_painel_detalhe", key=key, msg=data.get("error") or "Nao foi possivel encerrar."))
    return redirect(url_for("pings_painel_detalhe", key=key, ok="Ping encerrado. As vagas estao travadas."))


@app.route("/pings-painel/<key>/dividir", methods=["POST"])
def pings_painel_dividir(key):
    guild_id = shared.get_current_guild()
    modo = request.form.get("modo")
    try:
        profit = float((request.form.get("profit") or "0").replace(".", "").replace(",", "."))
    except ValueError:
        return redirect(url_for("pings_painel_detalhe", key=key))
    try:
        reparo = float((request.form.get("reparo") or "0").replace(".", "").replace(",", "."))
    except ValueError:
        reparo = 0
    taxa_pct_raw = (request.form.get("taxa_pct") or "").strip()
    taxa_pct = None
    if taxa_pct_raw:
        try:
            taxa_pct = float(taxa_pct_raw.replace(",", "."))
        except ValueError:
            taxa_pct = None
    data, status = _internal_call(
        "POST", f"/pings/{key}/split", guild_id=guild_id, modo=modo, profit=profit, reparo=reparo, taxa_pct=taxa_pct,
    )
    if status != 200:
        return redirect(url_for("pings_painel_detalhe", key=key, msg=data.get("error") or "Nao foi possivel dividir o loot."))
    return redirect(url_for("pings_painel_detalhe", key=key, ok="Loot processado e postado no topico do ping."))


@app.route("/legacy/enter")
def legacy_enter():
    """Ponte entre o dashboard novo (Bearer token no localStorage) e o painel
    avancado legado (cookie, HTML server-side). Valida token+guilda igual a
    API, seta os cookies de sessao do painel legado e manda pra pagina
    pedida (?next=, ex: /classes)."""
    token = request.args.get("token", "")
    guild_id = request.args.get("guild_id", "")
    next_path = request.args.get("next") or "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"

    sessions = _load_sessions()
    session = sessions.get(token)
    if not session or session.get("expires_at", 0) < time.time():
        return redirect(f"{FRONTEND_URL}/dashboard/config?erro=painel_legado_token_invalido")
    usable_ids = {w["id"] for w in _usable_workspaces(session)}
    if guild_id not in usable_ids:
        abort(403)

    resp = redirect(next_path)
    # samesite=None (com secure=True) pra funcionar tambem quando esse painel
    # e aberto dentro de um iframe embutido no dashboard novo (cross-site).
    resp.set_cookie(LEGACY_TOKEN_COOKIE, token, max_age=LEGACY_COOKIE_MAX_AGE, httponly=True, secure=True, samesite="None")
    resp.set_cookie(LEGACY_GUILD_COOKIE, guild_id, max_age=LEGACY_COOKIE_MAX_AGE, httponly=True, secure=True, samesite="None")
    return resp


@app.route("/auth/discord/login")
def discord_login():
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        return "Login com Discord ainda nao configurado no servidor (falta DISCORD_CLIENT_ID/SECRET).", 500
    state = secrets.token_urlsafe(16)
    authorize_url = (
        "https://discord.com/oauth2/authorize"
        f"?client_id={quote(DISCORD_CLIENT_ID)}"
        f"&redirect_uri={quote(DISCORD_REDIRECT_URI, safe='')}"
        "&response_type=code"
        "&scope=identify%20guilds"
        f"&state={state}"
    )
    resp = redirect(authorize_url)
    resp.set_cookie("oauth_state", state, max_age=300, httponly=True, secure=True, samesite="Lax")
    return resp


@app.route("/auth/discord/callback")
def discord_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    cookie_state = request.cookies.get("oauth_state")
    if not code or not state or state != cookie_state:
        return redirect(f"{FRONTEND_URL}/login?erro=oauth_state")

    try:
        token_resp = requests.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get("access_token")

        user_resp = requests.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"}, timeout=15,
        )
        guilds_resp = requests.get(
            "https://discord.com/api/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"}, timeout=15,
        )
        user_resp.raise_for_status()
        guilds_resp.raise_for_status()
    except requests.RequestException:
        return redirect(f"{FRONTEND_URL}/login?erro=oauth_falhou")

    user = user_resp.json()
    guilds = guilds_resp.json()

    # So guarda quais guildas o Discord diz que essa pessoa administra E ja
    # tem o bot instalado (elegveis) - NAO decide mais aqui quais dao acesso
    # de fato. Acesso real (admin_guild_ids/admin_guilds pro dashboard) e
    # sempre calculado na hora a partir de slots.json (_usable_workspaces),
    # pra compra de slot valer na hora sem precisar deslogar/logar de novo.
    tenant_ids = _tenant_guild_ids()
    eligible_guild_ids = []
    eligible_guilds = []
    for g in guilds:
        try:
            perms = int(g.get("permissions", 0))
        except (TypeError, ValueError):
            perms = 0
        is_admin = bool(perms & PERM_ADMINISTRATOR) or bool(perms & PERM_MANAGE_GUILD)
        if g.get("id") not in tenant_ids:
            continue
        nivel = "admin" if is_admin else None
        if not is_admin:
            # caller/gestor dado por pessoa ou por cargo: quem sabe os cargos e o bot
            resp, status = _internal_call("GET", "/nivel", guild_id=g["id"], user_id=user.get("id"))
            if status == 200:
                nivel = resp.get("nivel")
        if nivel:
            eligible_guild_ids.append(g["id"])
            eligible_guilds.append({"id": g["id"], "name": g.get("name"), "icon": g.get("icon"), "nivel": nivel})

    session_token = secrets.token_urlsafe(32)
    sessions = _load_sessions()
    sessions[session_token] = {
        "discord_id": user.get("id"),
        "username": user.get("username"),
        "global_name": user.get("global_name"),
        "avatar": user.get("avatar"),
        "eligible_guild_ids": eligible_guild_ids,
        "eligible_guilds": eligible_guilds,
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + SESSION_LIFETIME_SECONDS,
    }
    _save_sessions(sessions)

    resp = redirect(f"{FRONTEND_URL}/auth/callback?token={session_token}")
    resp.delete_cookie("oauth_state")
    return resp


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
        sessions = _load_sessions()
        if sessions.pop(token, None) is not None:
            _save_sessions(sessions)
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    s = require_session()
    admin_guilds = _usable_workspaces(s)
    admin_guild_ids = [w["id"] for w in admin_guilds]
    return jsonify({
        "discord_id": s["discord_id"],
        "username": s.get("username"),
        "global_name": s.get("global_name"),
        "avatar": s.get("avatar"),
        "admin_guild_ids": admin_guild_ids,
        "admin_guilds": admin_guilds,
        "locked_guilds": _locked_guilds(s),
    })


@app.route("/api/slots/personal", methods=["POST"])
def create_personal_slot():
    """Inicia assinatura (Stripe Checkout, modo subscription) de mais um
    espaco pessoal pro usuario logado. O slot so e criado de verdade quando
    o webhook confirmar o pagamento (checkout.session.completed) - aqui so
    devolve a URL do checkout pro front redirecionar."""
    session = require_session()
    discord_id = session["discord_id"]
    body = request.get_json(silent=True) or {}
    label = (body.get("label") or "Uso pessoal").strip()[:60] or "Uso pessoal"

    existing_personal = [sl for sl in shared.slots_for_user(discord_id) if sl.get("type") == "personal"]
    is_additional = bool(existing_personal)
    price_id = STRIPE_PRICE_PERSONAL_ADDITIONAL if is_additional else STRIPE_PRICE_PERSONAL
    amount = STRIPE_AMOUNT_PERSONAL_ADDITIONAL if is_additional else STRIPE_AMOUNT_PERSONAL

    checkout = _stripe_post("checkout/sessions", {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": 1,
        "success_url": f"{FRONTEND_URL}/dashboard?slot_checkout=sucesso",
        "cancel_url": f"{FRONTEND_URL}/dashboard?slot_checkout=cancelado",
        "client_reference_id": discord_id,
        "metadata[kind]": "personal",
        "metadata[discord_id]": discord_id,
        "metadata[label]": label,
        "subscription_data[metadata][kind]": "personal",
        "subscription_data[metadata][discord_id]": discord_id,
        "subscription_data[metadata][label]": label,
        **_pix_mandate_fields(amount),
        **_stripe_customer_field(discord_id),
    })
    return jsonify({"ok": True, "checkout_url": checkout["url"]})


@app.route("/api/slots/guild", methods=["POST"])
def create_guild_slot():
    """Inicia assinatura (Stripe Checkout) pra destravar uma guilda elegivel
    (o usuario administra ela no Discord e o bot ja ta instalado). Primeiro
    mes com desconto (cupom), renovacoes no preco cheio. O slot so e criado
    de verdade quando o webhook confirmar o pagamento - o slot destrava a
    guilda pra todo mundo que administra ela, nao so quem comprou."""
    session = require_session()
    discord_id = session["discord_id"]
    body = request.get_json(silent=True) or {}
    guild_id = str(body.get("guild_id") or "")
    eligible_ids = {g["id"] for g in session.get("eligible_guilds", [])}
    if not guild_id or guild_id not in eligible_ids:
        abort(403)

    existing = shared.slots_for_user(discord_id)
    already = any(sl.get("type") == "guild" and sl.get("guild_id") == guild_id for sl in existing)
    if already:
        return jsonify({"ok": True, "already_active": True})

    existing_guild_slots = [sl for sl in existing if sl.get("type") == "guild"]
    is_additional = bool(existing_guild_slots)
    price_id = STRIPE_PRICE_GUILD_ADDITIONAL if is_additional else STRIPE_PRICE_GUILD
    amount = STRIPE_AMOUNT_GUILD_ADDITIONAL if is_additional else STRIPE_AMOUNT_GUILD

    checkout_fields = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": 1,
    }
    if not is_additional:
        checkout_fields["discounts[0][coupon]"] = STRIPE_COUPON_GUILD_FIRST_MONTH

    checkout = _stripe_post("checkout/sessions", {
        **checkout_fields,
        "success_url": f"{FRONTEND_URL}/dashboard?slot_checkout=sucesso",
        "cancel_url": f"{FRONTEND_URL}/dashboard?slot_checkout=cancelado",
        "client_reference_id": discord_id,
        "metadata[kind]": "guild",
        "metadata[discord_id]": discord_id,
        "metadata[guild_id]": guild_id,
        "subscription_data[metadata][kind]": "guild",
        "subscription_data[metadata][discord_id]": discord_id,
        "subscription_data[metadata][guild_id]": guild_id,
        **_pix_mandate_fields(amount),
        **_stripe_customer_field(discord_id),
    })
    return jsonify({"ok": True, "checkout_url": checkout["url"]})


@app.route("/api/billing/summary")
def api_billing_summary():
    """Slots (assinaturas) reais do usuario logado - pessoal e de guilda -
    pra pagina de Cobranca do dashboard. Pra cada slot com assinatura Stripe
    de verdade, busca status/valor/proxima cobranca ao vivo na Stripe; slot
    sem stripe_subscription_id foi liberado manualmente (sem cobranca por
    tras, entao nao tem o que cancelar/gerenciar)."""
    session = require_session()
    discord_id = session["discord_id"]
    eligible_by_id = {g["id"]: g for g in session.get("eligible_guilds", [])}

    itens = []
    for slot in shared.all_slots_for_user(discord_id):
        item = {
            "slot_id": slot.get("id"),
            "type": slot.get("type"),
            "status": slot.get("status", "active"),
            "created_at": slot.get("created_at"),
            "manual": not bool(slot.get("stripe_subscription_id")),
        }
        if slot.get("type") == "guild":
            gid = slot.get("guild_id")
            info = eligible_by_id.get(gid, {})
            item["name"] = info.get("name") or (f"Guilda {gid}" if gid else "Guilda")
            item["icon"] = info.get("icon")
        else:
            item["name"] = slot.get("label") or "Uso pessoal"
            item["icon"] = None

        sub_id = slot.get("stripe_subscription_id")
        if sub_id:
            try:
                sub = _stripe_get(f"subscriptions/{sub_id}")
                price = (sub.get("items", {}).get("data") or [{}])[0].get("price", {})
                item["stripe_status"] = sub.get("status")
                item["cancel_at_period_end"] = sub.get("cancel_at_period_end")
                item["current_period_end"] = sub.get("current_period_end")
                item["amount"] = price.get("unit_amount")
                item["currency"] = price.get("currency")
                item["interval"] = (price.get("recurring") or {}).get("interval")
            except Exception:
                # Stripe fora do ar ou assinatura sumiu - mostra so o que ja
                # sabemos do slot local, sem derrubar a pagina inteira.
                item["stripe_status"] = None
        itens.append(item)

    itens.sort(key=lambda i: i.get("created_at") or 0)
    return jsonify({"ok": True, "slots": itens})


@app.route("/api/billing/portal", methods=["POST"])
def api_billing_portal():
    """Cria uma sessao do Customer Portal da Stripe (hospedado por eles) pro
    slot pedido e devolve a URL pro front redirecionar. La a pessoa ve
    faturas, troca cartao/Pix e cancela - cancelamento cai no webhook
    customer.subscription.deleted, que ja desativa o slot sozinho."""
    session = require_session()
    discord_id = session["discord_id"]
    body = request.get_json(silent=True) or {}
    slot_id = body.get("slot_id")

    alvo = next(
        (s for s in shared.all_slots_for_user(discord_id) if s.get("id") == slot_id),
        None,
    )
    if not alvo or not alvo.get("stripe_customer_id"):
        abort(404)

    portal = _stripe_post("billing_portal/sessions", {
        "customer": alvo["stripe_customer_id"],
        "return_url": f"{FRONTEND_URL}/dashboard/billing",
    })
    return jsonify({"ok": True, "portal_url": portal["url"]})


@app.route("/api/saldo/me")
def api_saldo_me():
    """Saldo acumulado do usuario logado na guilda ativa (workspace pessoal
    nao tem saldo - loot saldado so existe pra guilda)."""
    session, guild_id = require_guild_session()
    discord_id = str(session["discord_id"])
    data = shared.load_saldos(guild_id)
    entrada = data.get("saldos", {}).get(discord_id)
    valor = entrada.get("valor", 0.0) if entrada else 0.0
    nivel = _nivel_site(session, guild_id)
    return jsonify({"ok": True, "valor": valor, "gestor": nivel in ("admin", "gestor"), "nivel": nivel})


def _nivel_site(session, guild_id):
    """admin / gestor / caller / None dessa pessoa nesse servidor. Pergunta pro
    bot (ele sabe os cargos reais); se o bot estiver fora, usa o que ficou
    guardado no login."""
    if str(guild_id).startswith("user_"):
        return "admin"
    resp, status = _internal_call("GET", "/nivel", guild_id=guild_id, user_id=session["discord_id"])
    if status == 200:
        return resp.get("nivel")
    for g in session.get("eligible_guilds", []):
        if str(g.get("id")) == str(guild_id):
            return g.get("nivel") or "admin"
    if shared.is_saldo_gestor(guild_id, session["discord_id"]):
        return "gestor"
    return None


def _require_saldo_gestor(session, guild_id):
    """403 se a pessoa nao for gestor de saldo nem admin desse servidor."""
    if _nivel_site(session, guild_id) not in ("admin", "gestor"):
        abort(403)


def _require_slot_owner(session, guild_id):
    """403 se a pessoa nao for admin do servidor (quem decide a equipe)."""
    if _nivel_site(session, guild_id) != "admin":
        abort(403)


@app.route("/api/saldo/todos")
def api_saldo_todos():
    """Saldo de todos os membros que ja tiveram loot saldado nessa guilda -
    so pra gestor de saldo ver (pagina de configuracao do dashboard)."""
    session, guild_id = require_guild_session()
    _require_saldo_gestor(session, guild_id)
    data = shared.load_saldos(guild_id)
    itens = [
        {"discord_id": uid, "nome": info.get("nome"), "valor": info.get("valor", 0.0)}
        for uid, info in data.get("saldos", {}).items()
    ]
    itens.sort(key=lambda i: i["valor"], reverse=True)
    return jsonify({"ok": True, "saldos": itens, "sou_gestor": True})


@app.route("/api/saldo/quitar", methods=["POST"])
def api_saldo_quitar():
    """Da baixa (zera ou desconta um valor) no saldo de um membro - so pra
    gestor de saldo."""
    session, guild_id = require_guild_session()
    _require_saldo_gestor(session, guild_id)
    body = request.get_json(silent=True) or {}
    discord_id = body.get("discord_id")
    if not discord_id:
        abort(400)
    valor = body.get("valor")
    try:
        valor = float(valor) if valor not in (None, "") else None
    except (TypeError, ValueError):
        abort(400)
    novo_saldo = shared.quitar_saldo(
        guild_id, discord_id, session["discord_id"],
        por_nome=session.get("username") or session.get("global_name"),
        valor=valor,
    )
    return jsonify({"ok": True, "novo_saldo": novo_saldo})


@app.route("/api/saldo/gestores")
def api_saldo_gestores():
    """Lista de gestores de saldo dessa guilda (o dono do slot + os extras
    cadastrados). So visivel pra quem ja e gestor (senao vazaria pra
    qualquer membro logado quem tem esse poder)."""
    session, guild_id = require_guild_session()
    _require_saldo_gestor(session, guild_id)
    dono = shared.owner_discord_id_for_guild(guild_id)
    extras = shared.load_saldo_gestores(guild_id)
    saldos = shared.load_saldos(guild_id).get("saldos", {})

    def _nome(uid):
        return (saldos.get(str(uid)) or {}).get("nome") or str(uid)

    return jsonify({
        "ok": True,
        "dono": {"discord_id": dono, "nome": _nome(dono)} if dono else None,
        "extras": [{"discord_id": str(g), "nome": _nome(g)} for g in extras],
        "sou_dono": dono is not None and str(session["discord_id"]) == str(dono),
    })


@app.route("/api/saldo/gestores", methods=["POST"])
def api_saldo_gestores_add():
    """Adiciona um discord_id como gestor de saldo extra - so o dono do
    slot de guilda (o contratante) pode fazer isso."""
    session, guild_id = require_guild_session()
    _require_slot_owner(session, guild_id)
    body = request.get_json(silent=True) or {}
    discord_id = str(body.get("discord_id") or "").strip()
    if not discord_id or not discord_id.isdigit():
        abort(400)
    gestores = [str(g) for g in shared.load_saldo_gestores(guild_id)]
    if discord_id not in gestores:
        gestores.append(discord_id)
        shared.save_saldo_gestores(gestores, guild_id)
    return jsonify({"ok": True})


@app.route("/api/saldo/gestores/<discord_id>", methods=["DELETE"])
def api_saldo_gestores_remove(discord_id):
    """Remove um gestor de saldo extra - so o dono do slot de guilda pode."""
    session, guild_id = require_guild_session()
    _require_slot_owner(session, guild_id)
    gestores = [str(g) for g in shared.load_saldo_gestores(guild_id)]
    discord_id = str(discord_id)
    if discord_id in gestores:
        gestores.remove(discord_id)
        shared.save_saldo_gestores(gestores, guild_id)
    return jsonify({"ok": True})


@app.route("/api/saldo/ajustar", methods=["POST"])
def api_saldo_ajustar():
    """Adiciona (valor > 0) ou remove (valor < 0) prata do saldo de alguem.
    Gestor de saldo ou admin."""
    session, guild_id = require_guild_session()
    _require_saldo_gestor(session, guild_id)
    body = request.get_json(silent=True) or {}
    discord_id = str(body.get("discord_id") or "").strip()
    if not discord_id.isdigit():
        abort(400)
    try:
        valor = float(body.get("valor"))
    except (TypeError, ValueError):
        abort(400)
    if valor == 0:
        abort(400)
    novo = shared.ajustar_saldo(
        guild_id, discord_id, (body.get("nome") or "").strip()[:80] or None, valor,
        (body.get("motivo") or "").strip()[:120] or "Ajuste pelo site",
        session["discord_id"], por_nome=session.get("global_name") or session.get("username"),
    )
    return jsonify({"ok": True, "novo_saldo": novo})


@app.route("/api/saldo/historico")
def api_saldo_historico():
    session, guild_id = require_guild_session()
    _require_saldo_gestor(session, guild_id)
    hist = shared.load_saldos(guild_id).get("historico", [])[-100:]
    return jsonify({"ok": True, "historico": list(reversed(hist))})


@app.route("/api/equipe")
def api_equipe():
    """Quem e caller e quem e gestor de saldo (pessoas e cargos), com nomes, e
    o nivel de quem esta vendo. Qualquer um da equipe pode ver; so admin muda."""
    session, guild_id = require_guild_session()
    nivel = _nivel_site(session, guild_id)
    if nivel is None:
        abort(403)
    perms = shared.load_permissoes(guild_id)
    ids = sorted({p for n in perms.values() for p in n["pessoas"]})
    nomes, cargos = {}, []
    if ids:
        resp, status = _internal_call("GET", "/membros", guild_id=guild_id, ids=",".join(ids))
        if status == 200:
            nomes = {m["id"]: m for m in resp.get("membros", [])}
    resp, status = _internal_call("GET", "/cargos", guild_id=guild_id)
    if status == 200:
        cargos = resp.get("cargos", [])
    cargo_por_id = {c["id"]: c for c in cargos}
    out = {}
    for n, bloco in perms.items():
        out[n] = {
            "pessoas": [{"id": p, "nome": (nomes.get(p) or {}).get("nome") or p, "avatar": (nomes.get(p) or {}).get("avatar")} for p in bloco["pessoas"]],
            "cargos": [cargo_por_id.get(c) or {"id": c, "nome": "cargo apagado", "cor": None} for c in bloco["cargos"]],
        }
    return jsonify({"ok": True, "meu_nivel": nivel, "equipe": out, "cargos_do_servidor": cargos})


@app.route("/api/equipe", methods=["POST"])
def api_equipe_alterar():
    """{nivel: caller|gestor, tipo: pessoas|cargos, id, acao: adicionar|remover}. So admin."""
    session, guild_id = require_guild_session()
    _require_slot_owner(session, guild_id)
    body = request.get_json(silent=True) or {}
    nivel, tipo = body.get("nivel"), body.get("tipo")
    alvo = str(body.get("id") or "").strip()
    if nivel not in shared.NIVEIS_PERMISSAO or tipo not in ("pessoas", "cargos") or not alvo.isdigit():
        abort(400)
    shared.set_permissao(guild_id, nivel, tipo, alvo, adicionar=body.get("acao") != "remover")
    return jsonify({"ok": True})


@app.route("/api/membros")
def api_membros():
    """Busca membros do servidor pelo nome (pra escolher quem liberar ou
    ajustar saldo). So pra quem e da equipe."""
    session, guild_id = require_guild_session()
    if _nivel_site(session, guild_id) is None:
        abort(403)
    resp, status = _internal_call("GET", "/membros", guild_id=guild_id, q=(request.args.get("q") or "")[:40])
    return jsonify(resp), status


@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    """Recebe eventos da Stripe (assinatura verificada via HMAC, nao Bearer/
    cookie - por isso esta em _PUBLIC_PATHS). checkout.session.completed
    cria o slot de verdade (so aqui, nunca no clique do botao); cancelamento
    ou falha de cobranca desativa. Idempotente: reentrega do mesmo evento
    (a Stripe reenvia se nao receber 200) nao duplica slot nem sobrescreve
    um cancelamento com dado velho."""
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    if not STRIPE_WEBHOOK_SECRET or not _verify_stripe_signature(payload, sig_header, STRIPE_WEBHOOK_SECRET):
        abort(400)

    event = json.loads(payload)
    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        discord_id = meta.get("discord_id")
        kind = meta.get("kind")
        subscription_id = obj.get("subscription")
        if discord_id and kind and subscription_id:
            slots = shared.load_slots()
            if not any(s.get("stripe_subscription_id") == subscription_id for s in slots):
                new_slot = {
                    "id": f"slot_{secrets.token_hex(8)}",
                    "owner_discord_id": discord_id,
                    "type": kind,
                    "status": "active",
                    "source": "stripe",
                    "stripe_subscription_id": subscription_id,
                    "stripe_customer_id": obj.get("customer"),
                    "created_at": int(time.time()),
                }
                if kind == "guild":
                    new_slot["guild_id"] = meta.get("guild_id")
                else:
                    new_slot["workspace_id"] = shared.personal_tenant_id(f"{discord_id}_{secrets.token_hex(4)}")
                    new_slot["label"] = meta.get("label") or "Uso pessoal"
                slots.append(new_slot)
                shared.save_slots(slots)

    elif etype == "customer.subscription.deleted":
        subscription_id = obj.get("id")
        slots = shared.load_slots()
        changed = False
        for s in slots:
            if s.get("stripe_subscription_id") == subscription_id and s.get("status") != "canceled":
                s["status"] = "canceled"
                changed = True
        if changed:
            shared.save_slots(slots)

    elif etype == "invoice.payment_failed":
        subscription_id = obj.get("subscription")
        slots = shared.load_slots()
        changed = False
        for s in slots:
            if s.get("stripe_subscription_id") == subscription_id and s.get("status") == "active":
                s["status"] = "past_due"
                changed = True
        if changed:
            shared.save_slots(slots)

    elif etype == "customer.subscription.updated":
        subscription_id = obj.get("id")
        stripe_status = obj.get("status")
        if stripe_status == "active":
            slots = shared.load_slots()
            changed = False
            for s in slots:
                if s.get("stripe_subscription_id") == subscription_id and s.get("status") in ("past_due", "canceled"):
                    s["status"] = "active"
                    changed = True
            if changed:
                shared.save_slots(slots)

    return jsonify({"received": True})


# =========================================================================
# API JSON (consumida pelo site novo, hospedado fora - por isso o CORS acima)
# =========================================================================

@app.route("/api/health")
def api_health():
    return jsonify({"ok": True})


@app.route("/api/overview")
def api_overview():
    classes = shared.load_classes()
    templates = shared.load_templates()
    pings = shared.load_pings()
    reputacao = shared.load_reputation()

    pings_abertos = [p for p in pings.values() if not p.get("encerrado")]
    avaliacoes = reputacao.get("avaliacoes", [])
    positivos = sum(1 for a in avaliacoes if a.get("positivo"))
    total_avaliacoes = len(avaliacoes)
    pct_positivo_geral = round((positivos / total_avaliacoes) * 100, 1) if total_avaliacoes else None

    return jsonify({
        "classes_count": len(classes),
        "compositions_count": len(templates),
        "active_pings_count": len(pings_abertos),
        "total_pings_count": len(pings),
        "total_ratings": total_avaliacoes,
        "avg_reputation_pct": pct_positivo_geral,
    })


@app.route("/api/classes")
def api_classes():
    classes = shared.load_classes()
    return jsonify([
        {
            "id": c["id"],
            "nome": c["nome"],
            "cargo": c.get("cargo"),
            "vagas": c.get("vagas"),
            "arma_nome": c.get("arma_nome"),
            "item_id": c.get("item_id"),
        }
        for c in classes
    ])


@app.route("/api/compositions")
def api_compositions():
    templates = shared.load_templates()
    return jsonify([
        {
            "id": t["id"],
            "nome": t["nome"],
            "slots_count": len(t.get("slots", [])),
        }
        for t in templates
    ])


@app.route("/api/pings")
def api_pings():
    session, guild_id = require_guild_session()
    pings = shared.load_pings(guild_id)
    limit = request.args.get("limit", "50")
    limit = int(limit) if limit.isdigit() else 50

    itens = sorted(pings.items(), key=lambda kv: kv[1].get("criado_em", 0), reverse=True)[:limit]
    out = []
    for key, p in itens:
        confirmados = sum(len(f.get("claimed", [])) + len(f.get("extra", [])) for f in p.get("funcoes", []))
        vagas_total = sum(f.get("vagas", 0) for f in p.get("funcoes", []))
        out.append({
            "key": key,
            "titulo": p.get("titulo"),
            "local": p.get("local"),
            "fee": p.get("fee"),
            "quando": p.get("quando"),
            "criado_por_nome": p.get("criado_por_nome"),
            "criado_em": p.get("criado_em"),
            "encerrado": bool(p.get("encerrado")),
            "confirmados": confirmados,
            "vagas_total": vagas_total,
            "loot_dividido": bool(p.get("loot_dividido")),
        })
    return jsonify(out)


@app.route("/api/pings/<key>")
def api_ping_detail(key):
    session, guild_id = require_guild_session()
    pings = shared.load_pings(guild_id)
    p = pings.get(key)
    if not p:
        abort(404)
    return jsonify({
        "key": key,
        "titulo": p.get("titulo"),
        "local": p.get("local"),
        "fee": p.get("fee"),
        "quando": p.get("quando"),
        "criado_por_nome": p.get("criado_por_nome"),
        "criado_em": p.get("criado_em"),
        "encerrado": bool(p.get("encerrado")),
        "loot_dividido": bool(p.get("loot_dividido")),
        "loot_profit": p.get("loot_profit"),
        "loot_liquido": p.get("loot_liquido"),
        "funcoes": [
            {
                "nome": f.get("nome"),
                "emoji": f.get("emoji"),
                "vagas": f.get("vagas"),
                "confirmados": len(f.get("claimed", [])),
            }
            for f in p.get("funcoes", [])
        ],
    })


@app.route("/api/discord/channels")
def api_discord_channels():
    session, guild_id = require_guild_session()
    data, status = _internal_call("GET", "/channels", guild_id=guild_id)
    return jsonify(data), status


@app.route("/api/pings", methods=["POST"])
def api_pings_create():
    session, guild_id = require_guild_session()
    body = request.get_json(silent=True) or {}
    data, status = _internal_call(
        "POST", "/pings", guild_id=guild_id,
        canal_id=body.get("canal_id"),
        composicao_id=body.get("composicao_id"),
        titulo=body.get("titulo"),
        local=body.get("local"),
        fee=body.get("fee"),
        horario=body.get("horario"),
        vagas_texto=body.get("vagas_texto"),
        criado_por_discord_id=session.get("discord_id"),
        criado_por_nome=session.get("global_name") or session.get("username"),
    )
    return jsonify(data), status


@app.route("/api/pings/<key>", methods=["PUT"])
def api_pings_edit(key):
    session, guild_id = require_guild_session()
    body = request.get_json(silent=True) or {}
    payload = {k: body[k] for k in ("titulo", "local", "fee", "horario", "vagas") if k in body}
    data, status = _internal_call("PUT", f"/pings/{key}", guild_id=guild_id, **payload)
    return jsonify(data), status


@app.route("/api/pings/<key>/encerrar", methods=["POST"])
def api_pings_encerrar(key):
    session, guild_id = require_guild_session()
    data, status = _internal_call("POST", f"/pings/{key}/encerrar", guild_id=guild_id)
    return jsonify(data), status


@app.route("/api/pings/<key>/split", methods=["POST"])
def api_pings_split(key):
    session, guild_id = require_guild_session()
    body = request.get_json(silent=True) or {}
    modo = body.get("modo")
    if modo not in ("dividir", "comprador", "saldar"):
        return jsonify({"error": "modo invalido"}), 400
    try:
        profit = float(body.get("profit"))
    except (TypeError, ValueError):
        return jsonify({"error": "profit invalido"}), 400
    try:
        reparo = float(body.get("reparo") or 0)
    except (TypeError, ValueError):
        reparo = 0
    data, status = _internal_call(
        "POST", f"/pings/{key}/split", guild_id=guild_id, modo=modo, profit=profit, reparo=reparo,
    )
    return jsonify(data), status


@app.route("/api/reputation/leaderboard")
def api_reputation_leaderboard():
    limit = request.args.get("limit", "10")
    limit = int(limit) if limit.isdigit() else 10
    return jsonify(shared.reputation_leaderboard(limit=limit, min_ratings=1))


@app.route("/api/reputation/<discord_id>")
def api_reputation_user(discord_id):
    stats = shared.compute_reputation_stats(discord_id)
    if stats["total"] == 0:
        return jsonify(stats), 404
    return jsonify(stats)


# =========================================================================
# BUILDS PUBLICAS - pagina /builds-publicas do site, sem exigir login pra
# navegar/ver. So exige sessao pra votar (qualquer jogador com conta no
# STOMP, de qualquer guilda). Agrega builds_library.json de TODOS os
# workspaces (guildas + espacos pessoais) que tenham visibility="shared".
# =========================================================================

@app.route("/api/public/builds")
def api_public_builds():
    categoria = (request.args.get("categoria") or "").strip()
    ratings = shared.load_ratings()
    out = []
    for wid in shared.list_all_workspace_ids():
        for b in shared.load_build_library(wid):
            if b.get("visibility") != "shared":
                continue
            if categoria and categoria != "Todas" and b.get("categoria") != categoria:
                continue
            key = f"{wid}:{b['id']}"
            votes = list((ratings.get(key) or {}).values())
            avg = round(sum(votes) / len(votes), 1) if votes else None
            papel = b.get("papel")
            out.append({
                "public_id": key,
                "nome": b.get("nome"),
                "categoria": b.get("categoria") or "Sem categoria",
                "papel": papel,
                "papel_label": (BUILD_ROLES.get(papel) or {}).get("label"),
                "papel_cor": (BUILD_ROLES.get(papel) or {}).get("cor"),
                "item_id": b.get("item_id"),
                "equip": shared.get_build_equip(b),
                "shared_by_name": b.get("shared_by_name") or "Anonimo",
                "shared_at": b.get("shared_at") or 0,
                "avg_rating": avg,
                "rating_count": len(votes),
            })
    out.sort(key=lambda x: (-(x["avg_rating"] or 0), -x["rating_count"], -x["shared_at"]))
    return jsonify(out)


@app.route("/api/public/builds/<path:public_id>/rate", methods=["POST"])
def api_public_build_rate(public_id):
    session = require_session()
    body = request.get_json(silent=True) or {}
    try:
        stars = int(body.get("stars"))
    except (TypeError, ValueError):
        abort(400)
    if stars < 1 or stars > 5:
        abort(400)
    workspace_id, sep, build_id = public_id.partition(":")
    if not sep or not workspace_id or not build_id:
        abort(404)
    library = shared.load_build_library(workspace_id)
    if not any(b["id"] == build_id and b.get("visibility") == "shared" for b in library):
        abort(404)
    ratings = shared.load_ratings()
    ratings.setdefault(public_id, {})[session["discord_id"]] = stars
    shared.save_ratings(ratings)
    votes = list(ratings[public_id].values())
    avg = round(sum(votes) / len(votes), 1)
    return jsonify({"ok": True, "avg_rating": avg, "rating_count": len(votes)})


if __name__ == "__main__":
    port = int(os.environ.get("EVENTSITE_PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
