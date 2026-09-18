---
name: sheets-sync-setup
description: Nastaví náhradu Dataslayera pre PPC špecialistu - GitHub Actions cron (zadarmo, bez AI kreditov), ktorý mesačne syncuje Meta Ads a Microsoft Ads dáta do Google Sheets napojených na Looker Studio, v identickom formáte ako Dataslayer. Prevedie celým setupom - z DataslayerQueries tabu vygeneruje clients.json, vytvorí repo z template, nazdieľa sheety na service account, spustí backfill a overí čísla proti účtom. Použi vždy keď user povie "nastav mi sheets sync", "nahraď dataslayer", "sheets sync pre klienta", "pridaj klienta do sheets syncu", "napoj sheet na github sync", alebo chce automatizovať Meta/Microsoft Ads dáta do Google Sheets / Looker Studio.
---

# sheets-sync-setup

Cieľ: user má Google Sheets, ktoré doteraz plnil Dataslayer (Meta Ads / Microsoft Ads dáta pre Looker Studio). Ty ich prepojíš na GitHub Actions sync z template repa `robertmikus6-prog/meta-sheets-sync-template`, aby to bežalo zadarmo a bez Dataslayera.

Nikdy nepíš em dash (—). Komunikuj slovensky, kód anglicky.

## Čo potrebuješ od prostredia

- `gh` CLI prihlásené (`gh auth status`), so scope `workflow` (ak chýba: `gh auth refresh -s workflow`, user potvrdí kód v prehliadači)
- Google Workspace MCP (`gws_call`) na čítanie sheetov a zdieľanie cez Drive
- Meta Ads MCP (`graph_query`) na verifikáciu čísel (voliteľné, ale odporúčané)
- Ak niečo z toho chýba, daj userovi manuálne kroky z README template repa a pokračuj tam, kde vieš

## Postup

### 1. Zmapuj sheety
Pre každý sheet, ktorý user pošle:
1. `gws_call sheets spreadsheets.values get` range `DataslayerQueries!A1:AP10` → riadok 2+ obsahuje query. Vyčítaj: `Data source` (facebook / bing), `Accounts/views` (act_id alebo account_id), `Metrics`, `Dimensions`, `Special settings` (`action_report_time`), `Sheet name`.
2. Prečítaj hlavičku cieľového tabu (`Sheet1!A1:Z1`) a over poradie stĺpcov.
3. Zostav objekt do `clients.json` podľa mapovacej tabuľky v README template (sekcia 5). Kľúčové pravidlá:
   - dimension `ad_name` prítomná → `"level": "ad"`, inak `"adset"`
   - `link_click` → `inline_link_clicks`; `spend_eur` → `spend`
   - `offsite_conversion.X_actions` → `action:offsite_conversion.X`; `..._action_values` → `action_value:...`
   - `custom_metric_<ID>` → `action:offsite_conversion.custom.<ID>`
   - `action_report_time: conversion` v Special settings → prenes 1:1
   - Bing: `"source": "bing"`, `account_id`, stĺpce `year_month, campaign_name, adgroup_name, impressions, clicks, spend, ctr, cpc, conversions, conversion_rate`
   - poradie `columns` = poradie stĺpcov v hlavičke sheetu
4. Ukáž userovi hotový `clients.json` na potvrdenie.

### 2. Repo
```
gh repo create <nazov> --template robertmikus6-prog/meta-sheets-sync-template --private --clone
```
Do klonu zapíš `clients.json`, commitni, pushni. Ak template nie je dostupný (user nemá prístup), požiadaj Roberta Mikuša o pridanie ako collaborator.

### 3. Tokeny (robí user, ty ho navigueš)
Prevezmi presné kroky z README template: sekcia 2 (Google service account → secret `SERVICE_ACC`), sekcia 3 (Meta app + `ads_read` token → `META_TOKEN`), sekcia 4 (Microsoft, len ak treba: `MS_CLIENT_ID`, `MS_DEV_TOKEN`, `MS_REFRESH_TOKEN`, `GH_PAT`). Tokeny nikdy neprechádzajú cez tvoj kontext: user ich vloží cez GitHub web alebo `gh secret set NAZOV -R owner/repo < subor`. Pre MS refresh token spusti `get_refresh_token.py` lokálne, otvorí sa prehliadač, po prihlásení ulož súbor priamo do secretu a zmaž ho.

Over `gh secret list -R owner/repo`. Ak user pomenoval secrety inak, uprav názvy v `.github/workflows/sync.yml`, neprepisuj secrety.

### 4. Nazdieľaj sheety na service account
User ti pošle `client_email` service accountu. Pre každý sheet:
`gws_call drive permissions create` fileId=<sheet>, params `sendNotificationEmail=false`, body `{"role":"writer","type":"user","emailAddress":"<sa email>"}`.

### 5. Backfill a overenie
```
gh workflow run meta-sheets-sync -R owner/repo -f since=YYYY-MM-DD -f until=<vcera>
gh run watch <id> --exit-status; gh run view <id> --log | grep -E "===|Spend written|error|retry"
```
Pre každého klienta porovnaj "Spend written" s účtom za rovnaké obdobie (Meta: `graph_query act_X/insights` level=account, time_range; s `action_report_time` ak ho klient má). Rozdiel do pár centov = zaokrúhľovanie denných hodnôt, OK. Väčší rozdiel = zle namapované stĺpce alebo iný `action_report_time`, oprav a zopakuj (behy sú idempotentné).

### 6. Dokončenie
Povedz userovi: (a) vypnúť Dataslayer scheduled refresh pre prebrané sheety, (b) refreshnúť Looker, (c) Meta token obnovovať ~každé 2 mesiace (GitHub pošle mail pri zlyhaní), (d) nový klient = nový objekt v clients.json + zdieľanie sheetu.

## Známe pasce (z reálneho nasadenia)
- Dataslayer refreshoval o polnoci a posledný deň zapísal čiastočný. Backfill to opraví, netreba riešiť.
- Meta rate limity (code 4, 2, 1) rieši script retry; pri veľkých backfilloch ad-level účtov spúšťaj po jednom klientovi (`-f client="Nazov"`).
- Google Sheets občas vráti 503, script má retry; ak beh napriek tomu spadne, zopakuj ho.
- Microsoft API pri Monthly agregácii ignoruje deň v dátume, script preto ťahá denné dáta a scitava sám. Nemeň to.
- Microsoft nemá read-only scope, práva = práva prihláseného usera (použi Viewer usera).
- Azure redirect `http://localhost` bez portu akceptuje ľubovoľný port, helper používa 8080.
- GH_PAT musí mať Secrets **Read and write**, inak rotácia vráti 403.
- GitHub vypína cron po 60 dňoch bez commitov, workflow to rieši keepalive commitom.
