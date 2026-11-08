# Gerador Robusto de Lista M3U (SS IPTV)

Gerador prático e automático de playlists M3U otimizadas para o **SS IPTV**, com:

- Fonte configurável (basta colar a URL)
- Análise heurística da página + suporte a categorias
- Detecção de “carregar mais” / paginação (com aviso)
- Mesclagem inteligente de categorias (sinônimos e maiúsculas/minúsculas)
- Deduplicação de canais pelo link do stream
- Teste de canais ativos antes de incluir
- Persistência: **não apaga** canais que ainda funcionam; só remove após **6 atualizações** consecutivas sem funcionar
- Formato otimizado para SS IPTV com `#EXTSIZE: medium` (botões médios)
- Pronto para GitHub Actions (atualiza a cada 6 horas)

---

## Como usar

### 1. Configure a fonte

Abra o arquivo `generator.py` e localize a linha:

```python
SOURCE_URL = "xxxxxxxxxxxxxxxxxx"
```

Substitua pelo endereço real da fonte (pode ser página HTML ou link direto de `.m3u` / `.m3u8`).

### 2. Suba no GitHub

1. Crie um repositório novo (público ou privado).
2. Faça upload de todos os arquivos desta pasta.
3. Vá em **Actions** e habilite os workflows (botão “I understand my workflows, enable them”).
4. O workflow roda automaticamente a cada 6 horas (UTC) e também pode ser disparado manualmente.

### 3. Pegue o link da playlist

Após a primeira execução bem-sucedida, a playlist estará em:

```
https://raw.githubusercontent.com/SEU_USUARIO/SEU_REPO/main/playlist.m3u
```

(ou use jsDelivr/CDN se preferir)

No SS IPTV: **Configurações → Conteúdo → Playlists externas → Adicionar** e cole o link acima.

---

## Como funciona a persistência

O arquivo `data/channels_state.json` guarda o histórico de todos os canais já encontrados.

| Situação | Comportamento |
|----------|---------------|
| Canal novo e funcionando | Entra na lista com `fail_count = 0` |
| Canal antigo continua funcionando | `fail_count` zerado, dados atualizados |
| Canal não encontrado ou offline nesta rodada | `fail_count` +1 |
| `fail_count` chega a 6 | Canal é removido definitivamente |

Assim a lista não fica “piscando” com canais intermitentes.

---

## Formato da lista gerada (SS IPTV)

```m3u
#EXTM3U size="medium"
#EXTINF:-1 group-title="Notícias" tvg-logo="...",Canal Exemplo
#EXTSIZE: medium
https://exemplo.com/stream.m3u8
```

- Categorias organizadas com `group-title`
- Tamanho de botão médio em todos os itens
- Ordenado por categoria + nome

---

## Categorias mescladas automaticamente

O script já trata vários sinônimos, por exemplo:

| Entradas possíveis | Categoria final |
|--------------------|-----------------|
| noticias, News, notícias, jornalismo | **Notícias** |
| desenhos, kids, infantil, criança, cartoon | **Infantil** |
| cinema, filmes, movies | **Filmes** |
| documentários, animais, natureza, viagem, travel, história | **Documentários** |
| esportes, sports, futebol | **Esportes** |
| música, music | **Música** |

Você pode editar o dicionário `CATEGORY_SYNONYMS` no início do `generator.py` para adicionar mais.

---

## Limitações e personalização avançada

A extração genérica funciona bem em:

- Playlists M3U diretas
- Páginas com links claros para `.m3u8` / streams
- Sites com categorias em menus simples

**Sites com muito JavaScript** (botões “Carregar mais” que só funcionam via AJAX, players complexos etc.) podem precisar de adaptação.

Nesses casos:

1. Abra a página no navegador e inspecione como os canais são carregados (Network → XHR/Fetch ou elementos HTML).
2. Edite a função `scrape_source()` ou `extract_links_heuristic()` em `generator.py` para capturar exatamente o padrão do site.
3. Se precisar de navegador real, adicione Playwright (já existe suporte no Actions, mas aumenta o tempo de execução).

Exemplo de ponto de extensão (comentário no código):

```python
# TODO: personalizar aqui para a fonte X
# Ex: parsear JSON de uma API interna do site
```

---

## Requisitos locais (opcional)

```bash
pip install -r requirements.txt
python generator.py
```

---

## Avisos importantes

- Use apenas fontes públicas e de conteúdo que você tenha direito de acessar.
- O teste de stream é prático (HTTP + checagem de m3u8), não é um player completo. Alguns streams protegidos por token/geo podem falhar no teste e ainda funcionar no player (ou vice-versa).
- Respeite os servidores das fontes (o script já tem delays e limites).

---

Feito para ser simples de manter e fácil de adaptar a qualquer fonte que você queira usar.
