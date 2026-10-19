"""Cria as builds da DG Avalon (prints do SkeletonHill) na biblioteca do STOMP
e a composicao 'DG Avalon Reduzida' no formato lista com loot por vaga.
Roda na VM, dentro de ~/eventbot. Pode rodar de novo: nao duplica."""
import copy, json, sys, time
sys.path.insert(0, "/home/ubuntu/eventbot")
import albion_shared as shared

GID = "576409638031851541"
shared.set_current_guild(GID)

def e(item, *swaps):
    return {"item": item, "swaps": list(swaps)}

DPS_BASE = {
    "mainhand": e("T8_2H_SCYTHE_CRYSTAL"),
    "head": e("T8_HEAD_LEATHER_SET3", "T8_HEAD_CLOTH_KEEPER"),
    "chest": e("T8_ARMOR_CLOTH_KEEPER"),
    "shoes": e("T8_SHOES_CLOTH_ROYAL", "T8_SHOES_LEATHER_DRAGON"),
    "food": e("T8_MEAL_STEW", "T8_MEAL_STEW_FISH"),
    "potion": e("T8_POTION_BERSERK"),
}

def dps(nome, mainhand_swaps, cape_swap, offhand=None):
    eq = copy.deepcopy(DPS_BASE)
    eq["mainhand"]["swaps"] = list(mainhand_swaps)
    eq["cape"] = e("T8_CAPEITEM_AVALON", cape_swap)
    if offhand:
        eq["offhand"] = e(None, *offhand)
    return {"nome": nome, "item_id": "T8_2H_SCYTHE_CRYSTAL", "papel": "dps", "equip": eq}

SKIP_CONS = e("T8_POTION_CLEANSE", "T3_POTION_MOB_RESET")
BREKA_CONS = e("T8_POTION_CLEANSE", "T3_POTION_MOB_RESET", "T8_POTION_TORNADO")
MONTARIA = e("UNIQUE_MOUNT_GIANT_HORSE_ADC", "T8_MOUNT_DIREBOAR_FW_LYMHURST_ELITE")
BREKA_ARMOR = {
    "head": e("T8_HEAD_PLATE_KEEPER"),
    "chest": e("T8_ARMOR_PLATE_SET2"),
    "shoes": e("T8_SHOES_PLATE_FEY"),
    "cape": e("T8_CAPEITEM_FW_FORTSTERLING"),
}

BUILDS = [
    dps("Foice de Cristal + Repetidor", ["T8_2H_REPEATINGCROSSBOW_UNDEAD"], "T8_CAPEITEM_FW_CAERLEON"),
    dps("Foice de Cristal + Xbow", ["T8_MAIN_1HCROSSBOW"], "T8_CAPEITEM_FW_CAERLEON", offhand=["T8_OFF_TORCH_CRYSTAL", "T8_OFF_LAMP_UNDEAD"]),
    dps("Foice de Cristal + Fire", ["T8_2H_FIRESTAFF"], "T8_CAPEITEM_MORGANA"),
    dps("Foice de Cristal + Frost", ["T8_MAIN_FROSTSTAFF_AVALON"], "T8_CAPEITEM_MORGANA", offhand=["T8_OFF_TORCH_CRYSTAL", "T8_OFF_LAMP_UNDEAD"]),
    {"nome": "Skip · Espadas Duplas", "item_id": "T8_2H_DUALSWORD", "papel": None, "equip": {
        "mainhand": e("T8_2H_DUALSWORD"), "head": e("T8_HEAD_PLATE_SET1"), "chest": e("T8_ARMOR_LEATHER_SET3"),
        "shoes": e("T8_SHOES_GATHERER_ORE"), "cape": e("T8_CAPEITEM_FW_FORTSTERLING"),
        "potion": SKIP_CONS, "mount": e("UNIQUE_MOUNT_GIANT_HORSE_ADC")}},
    {"nome": "Skip · Dessangrador", "item_id": "T8_MAIN_RAPIER_MORGANA", "papel": None, "equip": {
        "mainhand": e("T8_MAIN_RAPIER_MORGANA"), "offhand": e("T8_OFF_HORN_KEEPER"), "head": e("T8_HEAD_CLOTH_ROYAL"),
        "chest": e("T8_ARMOR_LEATHER_SET3"), "shoes": e("T8_SHOES_LEATHER_UNDEAD"), "cape": e("T8_CAPEITEM_FW_FORTSTERLING"),
        "potion": SKIP_CONS, "mount": e("UNIQUE_MOUNT_GIANT_HORSE_ADC")}},
    {"nome": "Breka · Cajado Arcano", "item_id": "T8_2H_ARCANESTAFF", "papel": None, "equip": dict(
        copy.deepcopy(BREKA_ARMOR), mainhand=e("T8_2H_ARCANESTAFF"), potion=BREKA_CONS, mount=MONTARIA)},
    {"nome": "Breka · Maça Pétrea", "item_id": "T8_MAIN_ROCKMACE_KEEPER", "papel": None, "equip": dict(
        copy.deepcopy(BREKA_ARMOR), mainhand=e("T8_MAIN_ROCKMACE_KEEPER", "T8_MAIN_MACE"),
        offhand=e("T8_OFF_JESTERCANE_HELL", "T8_OFF_HORN_KEEPER"), potion=BREKA_CONS, mount=MONTARIA)},
]

agora = int(time.time())
lib = shared.load_build_library(GID)
por_id = {}
for b in BUILDS:
    bid = shared._slug("dg avalon " + b["nome"])
    por_id[b["nome"]] = bid
    novo = {
        "id": bid, "nome": b["nome"], "item_id": b["item_id"], "arma_nome": None, "emoji_id": None,
        "equip": b["equip"], "visibility": "shared", "categoria": "Roaming Avalon", "papel": b["papel"],
        "shared_by_name": "jeverson", "shared_by_discord_id": "350433836816924693",
        "shared_workspace_id": GID, "shared_at": agora,
    }
    lib = [x for x in lib if x["id"] != bid] + [novo]
shared.save_build_library(lib, GID)

def build_do_slot(nome):
    b = next(x for x in lib if x["id"] == por_id[nome])
    c = {k: copy.deepcopy(b[k]) for k in ("nome", "item_id", "arma_nome", "emoji_id", "equip")}
    c["arma_nome"] = "Foice de Cristal do Ancião"
    return [c]

SLOTS = [
    ("main_tank", "Main Tank", "🛡️", "Sacola de prata + Mapas", None),
    ("off_tank", "Off Tank", "🛡️", "Artefatos + peito", None),
    ("main_heal", "Main Heal + Elmo Cavaleiro", "💚", "Cabeças", None),
    ("foice_repetidor_1", "Foice de Cristal + Repetidor", "🔫", "Armas Ranged", "Foice de Cristal + Repetidor"),
    ("foice_repetidor_2", "Foice de Cristal + Repetidor", "🔫", "Botas", "Foice de Cristal + Repetidor"),
    ("sc_armadura_real", "SC + Armadura Real", "🌑", "Bolsas + Capas", None),
    ("foice_xbow", "Foice de Cristal + Xbow", "🔫", "Armas", "Foice de Cristal + Xbow"),
    ("foice_fire", "Foice de Cristal + Fire", "🔫", "Armas Melee", "Foice de Cristal + Fire"),
    ("foice_frost", "Foice de Cristal + Frost", "🔫", "Off Hand", "Foice de Cristal + Frost"),
    ("raiz_dps", "Raiz DPS + Elmo Cavaleiro", "🐍", "Artefatos", None),
    ("scout", "Scout", "👁️", "Peso", None),
]
comp = {"id": "dg_avalon_reduzida", "nome": "DG Avalon Reduzida", "conteudo_id": None, "formato": "lista", "slots": []}
for ordem, (sid, nome, emoji, loot, build_nome) in enumerate(SLOTS, start=1):
    builds = build_do_slot(build_nome) if build_nome else []
    comp["slots"].append({
        "id": sid, "nome": nome, "cargo": nome, "vagas": 1, "ordem": ordem, "role_id": None,
        "item_id": builds[0]["item_id"] if builds else None, "emoji_id": None,
        "emoji": emoji, "loot": loot, "builds": builds,
    })
templates = shared.load_templates(GID)
templates = [t for t in templates if t["id"] != comp["id"]] + [comp]
shared.save_templates(templates, GID)
print("builds:", len(BUILDS), "| slots:", len(comp["slots"]))
print(shared.vagas_texto_da_comp(comp))

# nome do Cavalo Galante no catalogo do seletor (nao vinha no items_db)
db = shared.load_items_db()
mounts = db.setdefault("mount", [])
if not any(m["id"] == "UNIQUE_MOUNT_GIANT_HORSE_ADC" for m in mounts):
    mounts.append({"id": "UNIQUE_MOUNT_GIANT_HORSE_ADC", "nome": "Cavalo Galante"})
    shared._save_json(shared.ITEMS_DB_FILE, db)
    print("Cavalo Galante adicionado ao items_db")
