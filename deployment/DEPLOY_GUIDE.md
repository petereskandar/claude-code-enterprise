# Guida ai Rilasci

Questo documento descrive come aggiornare Lambda, SPA e template CloudFormation nel setup Poste Italiane.

---

## Struttura dei componenti deployabili

| Componente | Sorgente | Stack CFN | Script di deploy |
|---|---|---|---|
| Lambda `ClaudeCode-QuotaAdmin` | `deployment/infrastructure/lambda-functions/quota_admin/index.py` | `claude-code-quota-admin` | `deployment/scripts/deploy-admin-dashboard.sh` |
| SPA Admin Dashboard | `deployment/infrastructure/lambda-functions/quota_admin/spa/` | `claude-code-quota-admin` | `deployment/scripts/deploy-admin-dashboard.sh` |
| Lambda `ClaudeCode-UserDashboard` | `deployment/infrastructure/lambda-functions/user_dashboard/index.py` | `claude-code-user-dashboard` | `deployment/scripts/deploy-user-dashboard.sh` |
| SPA User Dashboard | `deployment/infrastructure/lambda-functions/user_dashboard/spa/` | `claude-code-user-dashboard` | `deployment/scripts/deploy-user-dashboard.sh` |

---

## Workflow Git

```
develop  →  (PR + merge)  →  main
```

- Tutto il lavoro avviene su `develop`
- Quando la feature/fix è pronta: aprire una PR develop → main e mergiarla
- **Non pushare mai direttamente su `main`**

---

## Come eseguire un deploy

Gli script fanno tutto in 4 step automatici:

1. **CloudFormation deploy** — allinea l'infrastruttura al template in repo (`--no-fail-on-empty-changeset`: se non ci sono modifiche al template, non fa nulla)
2. **Lambda update** — zippa `index.py`, lo carica su S3 e aggiorna la funzione
3. **SPA upload** — sostituisce i placeholder (`__API_ENDPOINT__`, `__AZURE_CLIENT_ID__`, `__AZURE_TENANT_ID__`) e carica i file statici su S3
4. **CloudFront invalidation** — invalida la cache per rendere immediatamente visibili le modifiche

```bash
# Admin dashboard
bash deployment/scripts/deploy-admin-dashboard.sh

# User dashboard
bash deployment/scripts/deploy-user-dashboard.sh

# Con profilo AWS diverso da quello di default
bash deployment/scripts/deploy-admin-dashboard.sh --profile mio-profilo
bash deployment/scripts/deploy-user-dashboard.sh --profile mio-profilo

# Oppure via env var
export AWS_PROFILE=mio-profilo
bash deployment/scripts/deploy-admin-dashboard.sh
```

---

## Come aggiornare la Lambda

1. Modifica `index.py` del componente interessato
2. Commit e push su `develop`
3. Esegui lo script di deploy

---

## Come aggiornare la SPA (frontend)

Modifica i file in `spa/` (`app.js`, `index.html`, `style.css`) e ricorda di **incrementare la versione** del file JS nell'HTML per forzare il cache-bust:

```html
<!-- index.html -->
<script src="app.js?v=6"></script>  <!-- incrementa v= -->
```

Poi esegui lo script di deploy.

---

## Come aggiornare un template CloudFormation

Modifica il template YAML:

```
deployment/infrastructure/quota-admin-dashboard.yaml
deployment/infrastructure/user-dashboard.yaml
```

Valida prima di applicare:

```bash
aws cloudformation validate-template \
  --template-body file://deployment/infrastructure/quota-admin-dashboard.yaml \
  --region eu-central-1 \
  --profile poste-claudeaws
```

Poi esegui lo script di deploy — lo step CloudFormation applicherà le modifiche al template automaticamente.

---

## Profilo AWS

Tutti i comandi usano il profilo `poste-claudeaws` (account `308657715154`, region `eu-central-1`) come default.

```bash
export AWS_PROFILE=poste-claudeaws
# oppure aggiungere --profile poste-claudeaws a ogni comando
```

---

## Checklist prima di mergere su main

- [ ] Testato su `develop` (admin console + user dashboard aperti nel browser)
- [ ] Versione JS incrementata se modificata la SPA
- [ ] Deploy script eseguito e CloudFront invalidato
- [ ] Nessun placeholder `__API_ENDPOINT__` o simili rimasti nel codice
- [ ] PR aperta e mergiata develop → main
