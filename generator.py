#!/usr/bin/env python3
"""
Gerador robusto de lista M3U otimizada para SS IPTV.
- Fonte configurável (placeholder https://www.olhosnatv.com.br/)
- Análise heurística da página + suporte a categorias e "carregar mais"
- Mesclagem inteligente de categorias (sinônimos / case-insensitive)
- Deduplicação por URL de stream
- Teste de canais ativos
- Persistência: só remove canal após 6 falhas consecutivas
- Saída com #EXTSIZE: medium (botões médios no SS IPTV)
- Pronto para GitHub Actions (atualiza a cada 6h)
"""

import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO PRINCIPAL - ALTERE AQUI
# ---------------------------------------------------------------------------
SOURCE_URL = "https://www.olhosnatv.com.br/"   # <-- COLOQUE A URL DA FONTE AQUI

# Arquivos de estado e saída
STATE_FILE = "data/channels_state.json"
OUTPUT_M3U = "playlist.m3u"
MAX_FAILS_BEFORE_REMOVE = 6          # Remove só depois de 6 atualizações sem funcionar
TEST_TIMEOUT = 12                    # segundos para testar cada stream
MAX_WORKERS = 12                     # paralelismo no teste de streams
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Mapeamento de sinônimos de categorias (tudo vira a chave canônica)
CATEGORY_SYNONYMS = {
    # Notícias
    "noticias": "Notícias",
    "notícias": "Notícias",
    "news": "Notícias",
    "informação": "Notícias",
    "informacao": "Notícias",
    "jornalismo": "Notícias",
    "atualidades": "Notícias",
    # Infantil / Kids
    "desenhos": "Infantil",
    "kids": "Infantil",
    "infantil": "Infantil",
    "criança": "Infantil",
    "crianca": "Infantil",
    "crianças": "Infantil",
    "criancas": "Infantil",
    "cartoon": "Infantil",
    "cartoons": "Infantil",
    "animação": "Infantil",
    "animacao": "Infantil",
    "animation": "Infantil",
    # Cinema / Filmes
    "cinema": "Filmes",
    "filmes": "Filmes",
    "movies": "Filmes",
    "filme": "Filmes",
    "movie": "Filmes",
    "series": "Séries",
    "séries": "Séries",
    "seriados": "Séries",
    "tv shows": "Séries",
    # Documentários / Natureza / etc.
    "documentários": "Documentários",
    "documentarios": "Documentários",
    "documentario": "Documentários",
    "documentary": "Documentários",
    "docs": "Documentários",
    "animais": "Documentários",
    "natureza": "Documentários",
    "nature": "Documentários",
    "viagem": "Documentários",
    "travel": "Documentários",
    "curiosidades": "Documentários",
    "história": "Documentários",
    "historia": "Documentários",
    "history": "Documentários",
    "ciência": "Documentários",
    "ciencia": "Documentários",
    "science": "Documentários",
    # Esportes
    "esportes": "Esportes",
    "esporte": "Esportes",
    "sports": "Esportes",
    "sport": "Esportes",
    "futebol": "Esportes",
    "football": "Esportes",
    # Música
    "música": "Música",
    "musica": "Música",
    "music": "Música",
    "clips": "Música",
    # Religioso
    "religioso": "Religioso",
    "religious": "Religioso",
    "gospel": "Religioso",
    "igreja": "Religioso",
    # Adulto (opcional, pode remover se não quiser)
    "adulto": "Adulto",
    "xxx": "Adulto",
    "adult": "Adulto",
    # Variedades / Entretenimento
    "variedades": "Variedades",
    "entretenimento": "Variedades",
    "entertainment": "Variedades",
    "geral": "Variedades",
    "general": "Variedades",
}

# Expressões que indicam botão de "carregar mais" / paginação
LOAD_MORE_PATTERNS = [
    r"carregar\s*mais",
    r"load\s*more",
    r"mostrar\s*mais",
    r"ver\s*mais",
    r"próxima",
    r"proxima",
    r"next",
    r"mais\s*canais",
    r"more\s*channels",
    r"page\s*\d+",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("m3u-generator")


# ---------------------------------------------------------------------------
# UTILITÁRIOS
# ---------------------------------------------------------------------------
def normalize_category(raw: str) -> str:
    """Normaliza e aplica sinônimos de categoria."""
    if not raw:
        return "Outros"
    key = raw.strip().lower()
    key = re.sub(r"\s+", " ", key)
    return CATEGORY_SYNONYMS.get(key, raw.strip().title())


def is_likely_stream_url(url: str) -> bool:
    """Heurística para decidir se uma URL parece ser um stream."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    url_l = url.lower()
    # Extensões e padrões comuns de IPTV
    stream_patterns = (
        ".m3u8", ".m3u", ".ts", ".mpd", ".mp4",
        "/live/", "/stream/", "/hls/", "/dash/",
        "playlist.m3u8", "index.m3u8", "master.m3u8",
        "channel=", "ch=", "id=", "token=",
    )
    return any(p in url_l for p in stream_patterns)


def clean_name(name: str) -> str:
    """Limpa nome do canal."""
    name = re.sub(r"\s+", " ", name or "").strip()
    name = re.sub(r"[|•·]+", " ", name)
    return name[:120] if name else "Canal Sem Nome"


def channel_id(url: str) -> str:
    """ID estável baseado na URL do stream (para deduplicação e estado)."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# EXTRAÇÃO DA FONTE
# ---------------------------------------------------------------------------
def fetch_page(url: str, session: requests.Session) -> Optional[str]:
    try:
        r = session.get(url, timeout=25, allow_redirects=True)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        log.warning("Falha ao baixar %s: %s", url, e)
        return None


def extract_from_m3u_content(content: str, default_category: str = "Geral") -> List[Dict]:
    """Parseia conteúdo M3U clássico."""
    channels = []
    current_name = None
    current_group = default_category
    current_logo = None

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            # Extrai group-title e tvg-logo se existirem
            group_match = re.search(r'group-title="([^"]*)"', line, re.I)
            logo_match = re.search(r'tvg-logo="([^"]*)"', line, re.I)
            name_match = re.search(r",(.+)$", line)
            current_group = normalize_category(group_match.group(1) if group_match else default_category)
            current_logo = logo_match.group(1) if logo_match else None
            current_name = clean_name(name_match.group(1) if name_match else "Canal")
        elif line and not line.startswith("#") and current_name:
            if is_likely_stream_url(line) or line.startswith(("http://", "https://", "rtmp://", "rtsp://")):
                channels.append({
                    "name": current_name,
                    "url": line.strip(),
                    "category": current_group,
                    "logo": current_logo,
                })
            current_name = None
            current_logo = None
    return channels


def extract_links_heuristic(html: str, base_url: str) -> List[Dict]:
    """
    Extração heurística genérica.
    Procura:
      - Links <a> com href de stream
      - Atributos data-src, data-url, data-stream etc.
      - JSON embutido com urls de m3u8
      - Textos próximos que parecem nomes de canais
    """
    soup = BeautifulSoup(html, "html.parser")
    found: Dict[str, Dict] = {}  # url -> channel dict (dedup)

    # 1. Links diretos
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if is_likely_stream_url(href):
            name = clean_name(a.get_text() or a.get("title") or os.path.basename(urlparse(href).path))
            cat = "Outros"
            # Tenta descobrir categoria pelo contexto (pai com classe category etc.)
            parent = a.find_parent(["div", "li", "section", "article"])
            if parent:
                for cls in parent.get("class", []) + [parent.get("id") or ""]:
                    if cls and any(k in cls.lower() for k in ("cat", "genre", "group", "tipo")):
                        cat = normalize_category(cls)
                        break
            found[href] = {"name": name, "url": href, "category": cat, "logo": None}

    # 2. Atributos data-* comuns em players
    for tag in soup.find_all(True):
        for attr in ("data-src", "data-url", "data-stream", "data-file", "data-m3u8", "src"):
            val = tag.get(attr)
            if val and is_likely_stream_url(val):
                href = urljoin(base_url, val)
                name = clean_name(
                    tag.get("title")
                    or tag.get("alt")
                    or tag.get_text(strip=True)
                    or "Canal"
                )
                found[href] = {"name": name, "url": href, "category": "Outros", "logo": None}

    # 3. Procura m3u8/json embutido em scripts
    for script in soup.find_all("script"):
        if not script.string:
            continue
        # URLs de m3u8
        for match in re.finditer(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', script.string):
            href = match.group(0).rstrip("\\/,;")
            found[href] = {
                "name": clean_name(os.path.basename(urlparse(href).path) or "Stream"),
                "url": href,
                "category": "Outros",
                "logo": None,
            }
        # Possíveis objetos JSON simples
        for match in re.finditer(r'\{[^{}]*"(?:url|src|file|stream)"\s*:\s*"(https?://[^"]+)"[^{}]*\}', script.string):
            try:
                obj = json.loads(match.group(0))
                url = obj.get("url") or obj.get("src") or obj.get("file") or obj.get("stream")
                if url and is_likely_stream_url(url):
                    name = clean_name(obj.get("title") or obj.get("name") or "Canal")
                    found[url] = {"name": name, "url": url, "category": "Outros", "logo": None}
            except Exception:
                pass

    return list(found.values())


def find_category_links(html: str, base_url: str) -> List[Tuple[str, str]]:
    """Tenta localizar links de categorias na página."""
    soup = BeautifulSoup(html, "html.parser")
    cats = []
    seen = set()

    # Links em menus / listas de categorias
    candidates = soup.select(
        "nav a, .menu a, .categories a, .category a, "
        ".nav a, .sidebar a, ul.cats a, .filter a, "
        "[class*='categor'] a, [class*='genre'] a, [id*='categor'] a"
    )
    for a in candidates:
        href = a.get("href")
        text = clean_name(a.get_text())
        if not href or not text or len(text) < 2:
            continue
        full = urljoin(base_url, href)
        if full in seen or full == base_url:
            continue
        # Evita links óbvios de navegação
        if any(x in text.lower() for x in ("home", "início", "inicio", "contato", "login", "sobre")):
            continue
        seen.add(full)
        cats.append((text, full))

    return cats


def has_load_more(html: str) -> bool:
    """Verifica se existe botão/link de carregar mais."""
    text = html.lower()
    for pat in LOAD_MORE_PATTERNS:
        if re.search(pat, text):
            return True
    return False


def scrape_source(source_url: str) -> List[Dict]:
    """
    Função principal de coleta.
    Estratégia:
      1. Se a URL terminar em .m3u / .m3u8 → trata como playlist direta.
      2. Caso contrário, baixa a página, tenta extrair canais e categorias.
      3. Para cada categoria encontrada, visita e coleta.
      4. Heurísticas genéricas + pontos de extensão para o usuário.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })

    all_channels: List[Dict] = []
    seen_urls: Set[str] = set()

    def add_channels(chs: List[Dict]):
        for ch in chs:
            u = ch["url"].strip()
            if u not in seen_urls and is_likely_stream_url(u):
                seen_urls.add(u)
                ch["category"] = normalize_category(ch.get("category") or "Outros")
                ch["name"] = clean_name(ch.get("name"))
                all_channels.append(ch)

    log.info("Coletando da fonte: %s", source_url)

    # Caso 1: a própria fonte é um arquivo M3U
    if any(source_url.lower().endswith(ext) for ext in (".m3u", ".m3u8", ".txt")):
        content = fetch_page(source_url, session)
        if content:
            add_channels(extract_from_m3u_content(content))
        return all_channels

    # Caso 2: página HTML
    html = fetch_page(source_url, session)
    if not html:
        log.error("Não foi possível obter conteúdo da fonte.")
        return []

    # Extração direta da página principal
    add_channels(extract_links_heuristic(html, source_url))

    # Tenta também se o conteúdo parecer M3U (alguns sites servem M3U com content-type text/html)
    if "#EXTM3U" in html or "#EXTINF:" in html:
        add_channels(extract_from_m3u_content(html))

    # Categorias
    categories = find_category_links(html, source_url)
    log.info("Encontradas %d possíveis categorias", len(categories))

    for cat_name, cat_url in categories[:40]:  # limite de segurança
        log.info("  → Categoria: %s (%s)", cat_name, cat_url)
        cat_html = fetch_page(cat_url, session)
        if not cat_html:
            continue

        # Extrai canais desta categoria
        chs = extract_links_heuristic(cat_html, cat_url)
        for c in chs:
            c["category"] = normalize_category(cat_name)
        add_channels(chs)

        # Também tenta parse M3U se aparecer
        if "#EXTINF:" in cat_html:
            add_channels(extract_from_m3u_content(cat_html, default_category=cat_name))

        # Observação: "carregar mais" dinâmico (JS) exige Playwright/Selenium.
        # Aqui registramos a presença para o usuário saber.
        if has_load_more(cat_html):
            log.warning(
                "Página de categoria '%s' parece ter botão 'carregar mais'. "
                "Para coletar tudo pode ser necessário adaptar o scraper com Playwright.",
                cat_name,
            )

        time.sleep(0.8)  # educado com o servidor

    log.info("Total bruto coletado (antes de teste/dedup): %d canais", len(all_channels))
    return all_channels


# ---------------------------------------------------------------------------
# TESTE DE STREAMS
# ---------------------------------------------------------------------------
def test_stream(url: str, session: requests.Session) -> bool:
    """
    Teste rápido e prático de se o stream responde.
    1. HEAD/GET com Range pequeno
    2. Se for m3u8, verifica se tem segmentos
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Range": "bytes=0-1024",
        "Accept": "*/*",
    }
    try:
        # Primeiro tenta HEAD
        r = session.head(url, timeout=TEST_TIMEOUT, allow_redirects=True, headers=headers)
        if r.status_code in (200, 206, 302, 301):
            ct = r.headers.get("Content-Type", "").lower()
            if any(x in ct for x in ("video", "mpegurl", "m3u", "application/vnd", "application/x-mpeg", "octet-stream")):
                return True

        # GET parcial
        r = session.get(url, timeout=TEST_TIMEOUT, allow_redirects=True, headers=headers, stream=True)
        if r.status_code not in (200, 206):
            return False

        # Se for playlist m3u8, lê um pouco e procura EXTINF ou segmentos
        if ".m3u8" in url.lower() or "mpegurl" in r.headers.get("Content-Type", "").lower():
            chunk = next(r.iter_content(4096), b"")
            text = chunk.decode("utf-8", errors="ignore")
            if "#EXTM3U" in text or "#EXTINF" in text or ".ts" in text:
                return True
            return False

        # Qualquer resposta 200/206 com conteúdo é considerada potencialmente viva
        return True
    except Exception:
        return False


def test_channels(channels: List[Dict]) -> List[Dict]:
    """Testa em paralelo e retorna só os que passaram."""
    if not channels:
        return []

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    alive = []

    log.info("Testando %d canais (timeout=%ds, workers=%d)...", len(channels), TEST_TIMEOUT, MAX_WORKERS)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_ch = {
            executor.submit(test_stream, ch["url"], session): ch
            for ch in channels
        }
        for i, future in enumerate(as_completed(future_to_ch), 1):
            ch = future_to_ch[future]
            try:
                ok = future.result()
            except Exception:
                ok = False
            if ok:
                alive.append(ch)
            if i % 20 == 0 or i == len(channels):
                log.info("  Progresso teste: %d/%d (vivos até agora: %d)", i, len(channels), len(alive))

    log.info("Canais vivos nesta coleta: %d / %d", len(alive), len(channels))
    return alive


# ---------------------------------------------------------------------------
# ESTADO PERSISTENTE (não apaga canal que ainda funciona)
# ---------------------------------------------------------------------------
def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Não foi possível ler estado: %s", e)
    return {"channels": {}, "last_update": None}


def save_state(state: Dict):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def merge_with_state(new_alive: List[Dict], state: Dict) -> List[Dict]:
    """
    Lógica de persistência:
    - Canais novos vivos → entram com fail_count=0
    - Canais antigos que continuam vivos → fail_count=0 e atualiza dados
    - Canais antigos que não apareceram ou falharam → fail_count += 1
    - Remove apenas se fail_count >= MAX_FAILS_BEFORE_REMOVE
    """
    now = datetime.now(timezone.utc).isoformat()
    channels_state: Dict[str, Dict] = state.get("channels", {})
    new_by_id = {channel_id(ch["url"]): ch for ch in new_alive}

    # Atualiza os que vieram vivos nesta rodada
    for cid, ch in new_by_id.items():
        channels_state[cid] = {
            "name": ch["name"],
            "url": ch["url"],
            "category": ch["category"],
            "logo": ch.get("logo"),
            "fail_count": 0,
            "last_seen": now,
            "first_seen": channels_state.get(cid, {}).get("first_seen", now),
        }

    # Incrementa fail_count dos que não vieram nesta rodada
    to_remove = []
    for cid, info in channels_state.items():
        if cid not in new_by_id:
            info["fail_count"] = info.get("fail_count", 0) + 1
            if info["fail_count"] >= MAX_FAILS_BEFORE_REMOVE:
                to_remove.append(cid)
                log.info("Removendo canal após %d falhas: %s", info["fail_count"], info.get("name"))

    for cid in to_remove:
        del channels_state[cid]

    state["channels"] = channels_state
    state["last_update"] = now
    save_state(state)

    # Retorna lista final para gerar o M3U
    final = []
    for info in channels_state.values():
        final.append({
            "name": info["name"],
            "url": info["url"],
            "category": info["category"],
            "logo": info.get("logo"),
        })
    return final


# ---------------------------------------------------------------------------
# GERAÇÃO DO M3U (SS IPTV friendly)
# ---------------------------------------------------------------------------
def generate_m3u(channels: List[Dict], output_path: str):
    """
    Gera M3U otimizado para SS IPTV:
    - #EXTM3U com size="medium" (padrão)
    - Cada item com #EXTSIZE: medium
    - group-title para categorias
    """
    # Ordena por categoria e nome
    channels_sorted = sorted(channels, key=lambda c: (c["category"].lower(), c["name"].lower()))

    lines = [
        '#EXTM3U size="medium"',
        f'# Gerado automaticamente em {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}',
        f'# Total de canais: {len(channels_sorted)}',
        '',
    ]

    current_cat = None
    for ch in channels_sorted:
        cat = ch["category"]
        if cat != current_cat:
            lines.append(f'# --- {cat} ---')
            current_cat = cat

        logo_attr = f' tvg-logo="{ch["logo"]}"' if ch.get("logo") else ""
        # EXTINF com group-title
        lines.append(
            f'#EXTINF:-1 group-title="{cat}"{logo_attr},{ch["name"]}'
        )
        # Tamanho médio dos botões (SS IPTV)
        lines.append("#EXTSIZE: medium")
        lines.append(ch["url"])
        lines.append("")

    content = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    log.info("Playlist salva em %s (%d canais)", output_path, len(channels_sorted))


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    if SOURCE_URL.strip() in ("", "xxxxxxxxxxxxxxxxxx"):
        log.error(
            "SOURCE_URL ainda está com o placeholder!\n"
            "Abra generator.py e substitua 'xxxxxxxxxxxxxxxxxx' pela URL real da fonte."
        )
        return 1

    log.info("=== Iniciando gerador M3U ===")
    log.info("Fonte: %s", SOURCE_URL)

    # 1. Coleta
    raw_channels = scrape_source(SOURCE_URL)

    # 2. Deduplicação extra por URL (já feita na coleta, mas garante)
    unique = {}
    for ch in raw_channels:
        unique[ch["url"]] = ch
    raw_channels = list(unique.values())

    # 3. Testa os canais novos
    alive = test_channels(raw_channels)

    # 4. Mescla com estado persistente
    state = load_state()
    final_channels = merge_with_state(alive, state)

    # 5. Gera M3U
    generate_m3u(final_channels, OUTPUT_M3U)

    # Estatísticas por categoria
    from collections import Counter
    cats = Counter(c["category"] for c in final_channels)
    log.info("=== Resumo por categoria ===")
    for cat, cnt in sorted(cats.items(), key=lambda x: (-x[1], x[0])):
        log.info("  %s: %d", cat, cnt)

    log.info("Concluído. Canais finais na lista: %d", len(final_channels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
