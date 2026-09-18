# ads-sheets-sync (template)

Nahrada Dataslayer refreshov: GitHub Actions cron raz mesacne stiahne **Meta Ads** a/alebo
**Microsoft Ads** data a zapise ich do Google Sheets napojenych na Looker Studio,
v presne rovnakom formate stlpcov, aky generoval Dataslayer. Looker reporty ostanu
nedotknute. Nula mesacnych nakladov (GitHub free tier), ziadne AI kredity.

Beh: **2. den v mesiaci 05:00 UTC**, zapise cely predchadzajuci mesiac. Manualne
sa da spustit s lubovolnym rozsahom (backfill). Behy su idempotentne: riadky v danom
obdobi sa najprv zmazu a zapisu cerstve, takze nic sa neduplikuje a "neusadene"
polnocne data od Dataslayera sa opravia.

---

## Najrychlejsia cesta: nechaj to spravit Claude

1. Skopiruj priecinok `claude-skill/sheets-sync-setup/` do `~/.claude/skills/`
2. V Claude Code (s pripojenym Google Workspace MCP a Meta Ads MCP) napis:
   **"nastav mi sheets sync"** a posli mu linky na svoje Dataslayer sheety
3. Claude si sam precita `DataslayerQueries` tab v kazdom sheete, vygeneruje
   `clients.json`, vytvori repo z tohto template, nazdiela sheety na service
   account, spusti backfill a overi cisla proti uctom. Ty spravis len tokeny
   (kroky 2 a 3 nizsie), tie musia prejst cez tvoje prihlasenie.

Manualna cesta je popisana nizsie.

---

## Manualny setup (~30 min)

### 1. Repo

GitHub -> tento template -> **Use this template** -> Create a new repository ->
**Private**. Nazov napr. `ads-sheets-sync`.

### 2. Google service account (secret `SERVICE_ACC`)

Robot, ktorym GitHub zapisuje do tvojich sheetov. Ma pristup **iba** k sheetom,
ktore mu nazdielas.

1. [console.cloud.google.com](https://console.cloud.google.com) -> New Project (napr. `sheets-sync`)
2. APIs & Services -> Library -> **Google Sheets API** -> Enable
3. IAM & Admin -> Service Accounts -> **Create Service Account** (napr. `sheet-sync`),
   roly preskoc, Done
4. Otvor ho -> Keys -> Add Key -> Create new key -> **JSON** -> stiahne sa subor
5. **Cely obsah** JSON suboru (od `{` po `}`) vloz do repo secretu:
   Settings -> Secrets and variables -> Actions -> New repository secret -> `SERVICE_ACC`
6. **Kazdy sheet nazdielaj** (Share) na email service accountu
   (`sheet-sync@<projekt>.iam.gserviceaccount.com`) ako **Editor**
7. JSON subor zmaz z Downloads

### 3. Meta token (secret `META_TOKEN`)

1. [developers.facebook.com](https://developers.facebook.com) -> My Apps -> Create App ->
   typ **Business** (prazdna app, netreba review)
2. [Graph API Explorer](https://developers.facebook.com/tools/explorer/): vyber app,
   Add permission -> **iba `ads_read`** -> Generate Access Token -> vyber vsetky
   potrebne ad ucty
3. [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/):
   vloz token -> **Extend Access Token** (~60 dni) -> skopiruj predlzeny
4. Repo secret `META_TOKEN`

**Obnova kazde ~2 mesiace** (kroky 2-4, 5 minut). Pri expiracii beh spadne
s hlaskou "META TOKEN EXPIROVANY" a GitHub posle mail. Token s `ads_read` vie
iba citat statistiky, nic viac.

System user token (nikdy neexpiruje) pouzi len ak su vsetky ucty pod jednym
Business Managerom.

### 4. Microsoft Ads (len ak mas MS Ads klienta)

Microsoft nema read-only scope: prava tokenu = prava prihlaseneho usera.
Pre read-only sa prihlas userom s rolou **Viewer** v Microsoft Advertising.

Secrety: `MS_CLIENT_ID` (Azure app, moze byt tá ista ako pre MCP; musi mat
redirect `http://localhost` a povolene public client flows), `MS_DEV_TOKEN`
(developer token), `MS_REFRESH_TOKEN` (vygeneruj lokalne:
`pip install requests && python3 get_refresh_token.py <MS_CLIENT_ID>`, prihlas sa
v prehliadaci, token sa zapise do `ms_refresh_token.txt`, uloz do secretu a subor zmaz).

Microsoft refresh token pri kazdom pouziti rotuje. Aby si ho script vedel sam
ulozit spat, pridaj secret **`GH_PAT`**: github.com/settings/personal-access-tokens
-> Generate new token (fine-grained) -> Only select repositories -> toto repo ->
Repository permissions -> **Secrets: Read and write**. Potom uz token nikdy neriesis.

### 5. clients.json

Jeden objekt = jeden sheet. Vsetko potrebne je v tabe **`DataslayerQueries`**
tvojho sheetu (stlpce Accounts/views, Metrics, Dimensions, Special settings).

| Dataslayer | clients.json |
|---|---|
| `"id":"act_123..."` v Accounts | `act_id` |
| Dimension `ad_name` pritomna | `"level": "ad"`, inak `"adset"` |
| Special settings `action_report_time: conversion` | `"action_report_time": "conversion"` |
| Dimensions `date`, `year_month`, `campaign_name`, `adset_name`, `ad_name` | rovnake nazvy |
| Metric `impressions` | `impressions` |
| Metric `link_click` | `inline_link_clicks` |
| Metric `inline_link_click_ctr` | `inline_link_click_ctr` |
| Metric `spend` / `spend_eur` | `spend` |
| Metric `clicks`, `unique_clicks`, `frequency` | rovnake nazvy |
| Metric `offsite_conversion.fb_pixel_purchase_actions` | `action:offsite_conversion.fb_pixel_purchase` |
| Metric `offsite_conversion.fb_pixel_purchase_action_values` | `action_value:offsite_conversion.fb_pixel_purchase` |
| Metric `offsite_conversion.fb_pixel_lead_actions` | `action:offsite_conversion.fb_pixel_lead` |
| Metric `offsite_conversion.fb_pixel_custom_actions` | `action:offsite_conversion.fb_pixel_custom` |
| Metric `custom_metric_<ID>` | `action:offsite_conversion.custom.<ID>` |

**Poradie v `columns` = poradie stlpcov v sheete** (over podla hlavicky riadku 1).
Ci su datumy v sheete ulozene ako date alebo text si script zisti sam.

Microsoft Ads (`"source": "bing"`): `account_id` z Accounts, stlpce `year_month`,
`campaign_name`, `adgroup_name`, `impressions`, `clicks`, `spend`, `ctr`, `cpc`,
`conversions`, `conversion_rate`. Data su mesacne.

### 6. Prvy beh a overenie

Actions -> sync -> **Run workflow** -> `since` = zaciatok, `until` = vcera,
`client` prazdne (vsetci). Potom porovnaj spend v sheete s uctom za rovnake obdobie
(sedi na centy; rozdiel par centov je zaokruhlovanie dennych hodnot, rovnake ako
mal Dataslayer).

### 7. Vypni Dataslayer

Pre kazdy prebrany sheet vypni scheduled refresh v Dataslayeri, inak si budu
navzajom prepisovat data.

---

## Prevadzka

- Automaticky 2. den v mesiaci. Pri zlyhani posle GitHub mail, oprava = opakovat beh
- Novy klient = novy objekt v `clients.json` + nazdielat sheet na service account
- Backfill lubovolneho obdobia = Run workflow so `since`/`until`
- Script ma retry na docasne chyby Meta (rate limity) aj Google Sheets (5xx)
- Znama past: Microsoft API pri mesacnej agregacii ignoruje den v datume, preto
  script taha denne data a mesiace scita sam
