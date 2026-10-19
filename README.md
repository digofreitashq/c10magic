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

### Executável (sem Python)

Para usar o C10 Magic como um programa único, que não precisa de Python
instalado, gere o `.exe` (uma vez, em uma máquina com o projeto):

```powershell
.\build_exe.bat          # gera dist\C10Magic.exe (cerca de 25 MB)
```

Copie `C10Magic.exe` para qualquer pasta e dê dois cliques: ele cria o banco,
sobe o servidor e abre o navegador (usa a porta 8000 ou a primeira livre
depois dela). Feche a janela do programa para encerrar. Os dados ficam na
pasta **`dados\`**, criada ao lado do `.exe`: para levar sua coleção e seus
decks, copie `c10magic.sqlite3` para dentro dela (com o programa fechado).
Na primeira abertura de uma carta, as tags do Scryfall são baixadas (cerca de
um minuto). Alguns antivírus desconfiam de executáveis do PyInstaller; se
isso acontecer, rode pelo Python normalmente.

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

1. **Perfil do comandante** (`cards/services/profile.py`), tirado do deck
   médio do EDHREC (ou, sem ele, das cartas mais jogadas com o comandante):
   proporção de ramp, compra, remoção e board wipe (pelas tags de função das
   cartas), curva de mana, quantidade de criaturas e **temas** (tags muito
   mais comuns nesses decks do que no Magic em geral). As funções do próprio
   comandante também contam (`COMMANDER_RULES`): um comandante que reduz o
   custo de criaturas valoriza criaturas caras.
2. **Candidatas:** o pool do EDHREC + as cartas mais populares do Scryfall
   para cada papel, tema e faixa de custo importante, dentro da identidade de
   cor, legais em Commander, sem terrenos.
3. **Nota:** % dos decks do comandante que usam a carta + sinergia (EDHREC);
   cartas só do Scryfall têm nota menor, pela popularidade geral. Bônus, na
   ordem de prioridade **sinergia > remoção > compra > board wipe > ramp**:
   temas do deck e o que o comandante favorece valem mais que qualquer papel;
   depois o melhor papel da carta; e cada função a mais (papel, tema) soma.
   Ramp tem a menor prioridade em tudo: quando as cotas não cabem, é o que
   mais perde; ao economizar, é piorado primeiro; a sobra do orçamento vai
   antes para sinergia; e carta só de ramp entra apenas pela cota de ramp.
4. **Esqueleto:** as cotas de papel do perfil; o resto por nota, seguindo a
   curva de mana e as criaturas dos decks reais.
5. **Orçamento** (só as mágicas; comandante e básicos não contam): troca a
   carta que mais economiza perdendo menos nota, mantendo papel, "é
   criatura" e faixa de custo, até caber. Depois aproxima a curva da dos
   decks reais e, com sobra, troca por cartas melhores da mesma faixa.
   Nenhuma carta sozinha passa de 30% do orçamento.
6. **Terrenos:** em decks de duas ou mais cores, até 15% do orçamento é
   reservado para terrenos que geram duas ou mais cores do comandante ou que
   buscam um básico (Terramorphic Expanse, Evolving Wilds, taplands, as que dão
   1 de vida, Command Tower…), no lugar de básicos: até 10 terrenos em duas
   cores, 14 em três, 16–18 em quatro ou cinco, sempre com pelo menos 20
   básicos. Com pouco dinheiro, vários baratos valem mais que um caro. O que
   não for usado volta para as mágicas. O resto são básicos, divididos pelos
   símbolos de mana das cartas (o comandante pesa 3×).

**Orçamento de até R$ 10** (`LOW_BUDGET_LIMIT`): o deck não leva pedras de
mana. Se o comandante não tem sinergia com terrenos, também não leva ramp: a
cota vai a zero e as vagas viram sinergia (cartas que fazem ramp e outra coisa
ainda entram pela outra função). Comandantes com sinergia de terrenos
(`landfall`, `lands-matter`, terreno extra, terrenos no cemitério… ver
`LAND_SYNERGY_TAGS`) mantêm o ramp. Com média abaixo de R$ 0,40 por carta, o
construtor também busca as cartas mais baratas de cada papel. Se mesmo
trocando o deck não couber, as cotas são liberadas da menor prioridade para a
maior (ramp primeiro).

O deck abre no editor com um relatório da montagem. Preços que ainda
não estavam no banco entram como estimativa pelo dólar e são buscados na
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
