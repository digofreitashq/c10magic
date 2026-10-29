# C10 Magic

Busca de cartas de Magic: The Gathering em toda a base do
[Scryfall](https://scryfall.com), com preço em reais da
[LigaMagic](https://www.ligamagic.com.br). As cartas da sua coleção
aparecem marcadas nos resultados.

## Dois modos de busca

- **Todas as cartas** (padrão): consulta o Scryfall e aceita toda a
  [sintaxe dele](https://scryfall.com/docs/syntax). As cartas que você tem
  ganham um selo verde com a quantidade. A marcação é feita por `oracle_id`
  (qualquer impressão conta) e, se não houver, pelo nome. O preço em reais
  vem da LigaMagic: o que já está no banco aparece na hora, e o que falta é
  buscado em segundo plano, com uma estimativa pelo dólar (`≈ R$`) até chegar.
  O filtro em R$ é convertido para US$ pela cotação do dia, porque o
  Scryfall só conhece preços em dólar.
- **Minha coleção**: busca só no banco local e filtra pelo preço real da
  LigaMagic.

Na página de qualquer carta do Scryfall dá para adicioná-la à coleção.

## Instalação

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py runserver
```

Acesse http://127.0.0.1:8000.

### Versão portátil (sem Python instalado)

Para usar o C10 Magic em um computador sem Python, gere o pacote portátil:

```powershell
.\build_portable.bat
```

Ele cria `dist\C10Magic-portatil\` e `dist\C10Magic-portatil.zip` (cerca de 22 MB). É o
**Python oficial embutido** (python.org) com as bibliotecas e o projeto como
arquivos comuns, sem executável novo. Extraia o zip numa pasta (não rode de
dentro dele) e dê dois cliques em **`C10 Magic.bat`**: o programa sobe e abre
o navegador (porta 8000 ou a primeira livre depois dela). Feche a janela
preta para encerrar. `Criar atalho na area de trabalho.bat` cria um atalho com
o ícone do C10 Magic.

Os dados ficam em `dados\`. Para levar sua coleção e seus decks, copie
`c10magic.sqlite3` para dentro dela (com o programa fechado). Na primeira
abertura de uma carta, as tags do Scryfall são baixadas (cerca de um minuto).

### Executável único (`C10Magic.exe`)

`.\build_exe.bat` gera `dist\C10Magic\C10Magic.exe` com o PyInstaller. Funciona, mas
**antivírus desconfiam de executáveis novos e sem assinatura**: o Avast mostra
o alerta `IDP.Generic` e executa o programa em modo isolado ("sandbox"), o que
pode impedir o navegador de acessar o servidor local. Só um certificado de
assinatura de código elimina isso. Prefira a versão portátil acima, ou envie o
`.exe` como falso positivo ao fabricante e adicione a pasta às exceções do
antivírus.

### Configuração

Nada fica no código: na primeira execução, uma chave secreta aleatória é
gerada em `.django_secret_key` (fora do git). Para outro ambiente, use
variáveis de ambiente:

| Variável | Padrão | Uso |
|---|---|---|
| `DJANGO_SECRET_KEY` | lida de `.django_secret_key` | chave secreta do Django |
| `DJANGO_DEBUG` | `1` | `0` em produção |
| `DJANGO_ALLOWED_HOSTS` | vazio | domínios, separados por vírgula |

O banco (`c10magic.sqlite3`), o `venv/` e os CSVs de `csv_import/` não vão
para o repositório.

## Carregando a coleção

Pela interface: **Coleção → Importar CSV da LigaMagic** (vários arquivos de
uma vez). Os dados são carregados em segundo plano.

Pela linha de comando:

```powershell
.\venv\Scripts\python.exe manage.py import_csv csv_import
.\venv\Scripts\python.exe manage.py update_cards
```

`update_cards` aceita `--limit N`, `--name texto` e `--force`.

## Atualização semanal

Na primeira vez, cada carta é carregada do Scryfall (dados, imagem, preço em
US$) e da LigaMagic (menor preço em R$). Depois disso, cada fonte só é
consultada de novo quando os dados tiverem mais de 7 dias
(`CARD_REFRESH_DAYS` em `c10magic/settings.py`). Uma falha de rede não conta
como atualização, então a carta é tentada de novo na próxima execução.

A mesma regra vale para as cartas fora da coleção: o JSON de cada carta vista
no Scryfall (`CachedCard`) e o preço da LigaMagic (`BrlPrice`) ficam no banco
e só são buscados de novo depois de uma semana. As páginas de resultado de
busca ficam em cache por 6 horas (`SCRYFALL_SEARCH_CACHE_SECONDS`).

### Decks

O menu **Decks** mostra os decks numa grade: capa (arte do comandante, ou de
qualquer carta do deck, pelo botão ▣ sobre a carta no editor), nome, cores e
datas de criação e atualização. O primeiro item, **+**, oferece **Gerar deck**
(abaixo) e **Cadastrar deck** (cria um deck vazio e abre o editor para colar a
lista). Na grade (menu ⋯) e no editor (Ações ▾): renomear (vazio volta ao
nome do comandante), orçamento, exportar (LigaMagic, Moxfield / MTG Arena,
Archidekt, texto simples), clonar, reconstruir (decks gerados) e excluir. No
editor também dá para substituir a lista de cartas.

### Gerar deck (montar um deck de Commander)

Em **Decks → + → Gerar deck**, escolha o comandante (e um segundo, para partner /
background / friends forever) e, se quiser, um orçamento em R$. O deck sai
com o(s) comandante(s), **40 terrenos básicos** e 59 mágicas (58 com dupla):

A ideia é ser um **espelho do deck médio do comandante no EDHREC**, dentro do
orçamento:

1. **Perfil** (`cards/services/profile.py`): quantas cartas de remoção,
   compra, board wipe e ramp (pelas tags de função) e quantas criaturas o deck
   médio tem; o resto das vagas é sinergia. Com orçamento (até R$ 500), usa os
   **decks baratos** do EDHREC (`commanders/<nome>/budget` e o deck médio
   deles). Sem deck médio, as cartas mais jogadas com o comandante.
2. **Candidatas:** as cartas da página do comandante no EDHREC + buscas no
   Scryfall por cartas da mesma função (papéis, temas do deck médio, tribo,
   equipamentos…), dentro da identidade de cor, legais em Commander, sem
   terrenos. Nenhuma carta sozinha passa de 30% do orçamento.
3. **Ordem:** a popularidade **com este comandante** (% dos decks dele que
   usam a carta + a sinergia do EDHREC). A popularidade geral no Magic não
   conta; cartas que não estão na página do comandante começam com nota 0.
   Desempates pequenos: temas do deck médio, tags escolhidas por você, tribo.
4. **Seleção:** as cotas de cada papel com as melhores cartas de cada um (uma
   carta com dois papéis fica no mais importante: remoção > compra > board
   wipe > ramp), depois as vagas de sinergia, mantendo as criaturas do deck
   médio (±3). Cartas "relentless" com sinergia (Tempest Hawk, Relentless
   Rats) entram com 8 cópias.
5. **Orçamento** (só as mágicas; comandante e básicos não contam): troca cada
   carta por uma mais barata **da mesma função e parecida** (mesmo papel,
   tribo, tipo, tags e cores; `similarity`), sempre a troca que mais economiza
   perdendo menos. Se não couber, as cotas são liberadas do ramp para a
   remoção. Com sobra, troca por cartas melhores da mesma função.
6. **Estratégias do comandante:**
   - **Tribal** (tag `typal-X` do comandante ou do deck médio, ou o tipo de
     criatura que você escolher): troca cartas de fora da tribo por criaturas
     da tribo que fazem a mesma função, primeiro as não-criaturas, mesmo com
     nota um pouco menor (`TRIBE_SWAP_MARGIN`); as criaturas podem passar das
     do deck médio em até 6.
   - **Voltron** (Voltron ou Equipment entre os dois temas principais do
     comandante no EDHREC): equipamentos e cartas que dão proteção
     (hexproof, indestrutível, proteção, ward) valem mais.
   - **Orçamento de até R$ 10** (`LOW_BUDGET_LIMIT`): sem ramp, a não ser que
     o comandante tenha sinergia com **terrenos** (landfall… o ramp vira só
     mágicas que põem terrenos em jogo ou deixam jogar mais de um terreno no
     turno, sem pedras de mana), com **artefatos** (ramp com pedras de mana)
     ou custe 6 ou mais de mana.
7. **Terrenos:** em decks de duas ou mais cores, até 15% do orçamento é
   reservado para terrenos que geram duas ou mais cores do comandante ou que
   buscam um básico (Terramorphic Expanse, Evolving Wilds, taplands, as que dão
   1 de vida, Command Tower…), no lugar de básicos: até 10 terrenos em duas
   cores, 14 em três, 16–18 em quatro ou cinco, sempre com pelo menos 20
   básicos. Com pouco dinheiro, vários baratos valem mais que um caro. O que
   não for usado volta para as mágicas. O resto são básicos, divididos pelos
   símbolos de mana das cartas (o comandante pesa 3×).

**Tags e tipos para valorizar** (opcional, antes de gerar): o formulário mostra
as tags de função do comandante e permite acrescentar qualquer outra tag
existente e um tipo de criatura, com autocompletar. Cartas com essas tags
ganham um desempate na ordem (`TAG_BONUS`); o botão **criatura** faz a tag só
contar em criaturas. Para trocá-las, abra o relatório do deck ("Montado pelo
Construtor"), mude as tags e/ou as proporções em **Ajustar tags e proporções**
e clique em **Gerar de novo**.

**Preço estimado:** carta sem preço da LigaMagic no banco entra com uma
estimativa a partir do dólar do Scryfall, **calibrada pela LigaMagic**
(`cards/services/exchange.py`, `estimate_brl`): por faixa de preço, quanto as
cartas que já têm os dois preços custam de fato na LigaMagic (as baratas
custam cerca de 25% a 35% de dólar × cotação). A calibração é refeita uma
vez por dia; vale para o construtor, a busca e o valor dos decks.

O deck abre no editor com um relatório da montagem. Preços que ainda
não estavam no banco entram como estimativa e são buscados na
LigaMagic em seguida; o botão **Reconstruir** monta de novo com os preços já
buscados. As regras ficam em `cards/services/builder.py` (`ROLES`,
`MAX_SHARE_OF_BUDGET`).

### Editor de deck

Em **Decks → + → Cadastrar deck**, cole a lista de um deck. Formatos aceitos:

- LigaMagic: `1 Fuga de Mana` (nomes em português ou inglês)
- Archidekt: `1x Vren, the Relentless (blb) [Commander{top}]`
- Moxfield / MTGA: `1 Sol Ring (CMM) 410`

O comandante vem da categoria `Commander` (Archidekt) ou do cabeçalho
`Comandante` / `Commander`; se não der para identificar, a página pede para
escolher. Sideboard e Maybeboard são ignorados.

A página do deck mostra:

- o comandante (imagem ao passar o mouse; clique abre a página dele);
- o **valor total em R$**, sempre atualizado, e um **orçamento** opcional;
- o **pool de sugestões do EDHREC** para o comandante, por categoria, sem as
  cartas que já estão no deck, com sinergia e % de decks que usam cada uma, e
  a comparação de tipos do deck com a média do EDHREC;
- as cartas do deck com imagens, agrupadas por tipo.

O botão **≈** sobre cada carta abre as sugestões de troca: primeiro cartas do
pool do EDHREC com o mesmo papel (tag de função), depois as mais populares do
Scryfall. Só aparecem cartas da identidade de cor do comandante, legais em
Commander, fora do deck e que cabem no orçamento. Cada uma mostra a diferença
de preço e tem o botão **Trocar**. A página do EDHREC fica no banco por uma
semana (`EdhrecPage`).

### Cartas similares

O botão **≈ Encontrar similares**, na página de cada carta, abre uma busca de
cartas parecidas que se atualiza enquanto você mexe nos controles:

- **Preço máximo em R$**: começa no preço da carta de referência.
- **Tags obrigatórias**: as tags de função da carta, com o número de cartas
  que têm cada uma. Começa marcado o papel mais específico da carta (lista
  `ROLE_TAGS` em `cards/services/similar.py`): `burn` para Lightning Bolt,
  `mana-dork` para Llanowar Elves, `removal-exile` para Swords to Plowshares.
- **Cores obrigatórias** e "sem cores além das da referência".
- **Tipos**: os oito tipos principais. Começa marcado o tipo da referência; o
  resultado precisa ter pelo menos um dos marcados (nenhum = qualquer tipo).
- Máxima diferença de valor de mana, "só na minha coleção" e ordenação.

A busca gerada aparece acima dos resultados e pode ser aberta na busca normal.

### Tags

A página de cada carta mostra as tags do [Scryfall Tagger](https://tagger.scryfall.com):
**Função** (oracle tags, como `removal` e `ramp`) e **Arte** (art tags). Cada tag
é um link para a busca `otag:` / `art:`, que também funciona em "Minha coleção".
A API de cartas não traz as tags, então a lista completa (cerca de 80 MB, por um
endpoint não documentado do Scryfall) é baixada uma vez por semana para o banco:
na primeira vez que uma página de carta é aberta, ou com
`manage.py sync_tags`. Leva cerca de um minuto e ocupa uns 35 MB no banco.

Para manter tudo em dia sem abrir a interface, agende `update_cards` no
Agendador de Tarefas do Windows (por exemplo, uma vez por dia): ele só busca
as cartas vencidas.

## Sintaxe de busca

Em **Todas as cartas** vale a sintaxe completa do Scryfall, mais `brl`
(convertido para `usd`). Em **Minha coleção** valem as chaves abaixo:

| Chave | Exemplo | Significado |
|---|---|---|
| `t:` | `t:legendary t:elf` | tipo |
| `o:` | `o:"draw a card"` | texto de regras |
| `c:` | `c:ur`, `c=ur`, `c<=ur`, `c:m`, `c:c` | cores (contém / exatas / no máximo / multicolor / incolor) |
| `id:` | `id:golgari`, `id<=esper` | identidade de cor (Commander) |
| `mv` / `cmc` | `mv<=3` | valor de mana |
| `pow` / `tou` | `pow>=4`, `tou>pow` | poder / resistência |
| `r:` | `r:m`, `r>=rare` | raridade |
| `s:` | `s:ltr` | edição |
| `kw:` | `kw:flying` | palavra-chave |
| `f:` / `banned:` | `f:pauper`, `banned:modern` | formato |
| `brl` | `brl<=10` | preço em reais |
| `usd` | `usd<1` | preço em dólar |
| `m:` | `m:{G}{G}`, `m:uu` | símbolos no custo |
| `is:` | `is:commander`, `is:vanilla`, `is:dfc`, `is:unpriced` | atalhos |
| `a:` `ft:` `year` `qtd` | `year<2000`, `qtd>=4` | artista, flavor, ano, quantidade |

`-` nega um termo, `or` combina grupos e `!"Nome"` busca o nome exato.
Os filtros avançados da tela combinam com a busca em texto.

## Testes

```powershell
.\venv\Scripts\python.exe manage.py test cards
```
