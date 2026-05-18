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

## Come aggiornare una Lambda

### 1. Modifica il codice

Edita il file `index.py` del componente interessato.

### 2. Commit e push su develop

```bash
git add deployment/infrastructure/lambda-functions/<componente>/index.py
git commit -m "fix/feat: descrizione modifica"
git push origin develop
```

### 3. Esegui lo script di deploy

```bash
# Admin dashboard
bash deployment/scripts/deploy-admin-dashboard.sh

# User dashboard
bash deployment/scripts/deploy-user-dashboard.sh
```

Lo script fa automaticamente:
- Zip del `index.py` → upload su S3 (`claude-code-s3-cfnartifactsbucket-0enb7bvsxlut`)
- `aws lambda update-function-code` sulla funzione
- Upload SPA con sostituzione placeholder (`__API_ENDPOINT__`, `__AZURE_CLIENT_ID__`, `__AZURE_TENANT_ID__`)
- Invalidazione CloudFront

> **Nota**: gli script **saltano il deploy CloudFormation** — lo step CFN va eseguito solo se si modifica il template YAML.

---

## Come aggiornare la SPA (frontend)

Modifica i file in `spa/` (`app.js`, `index.html`, `style.css`) e ricorda di **incrementare la versione** del file JS nell'HTML per forzare il cache-bust:

```html
<!-- index.html -->
<script src="app.js?v=6"></script>  <!-- incrementa v= -->
```

Poi esegui lo script di deploy come sopra — la SPA viene ricaricata e CloudFront invalidato automaticamente.

---

## Come aggiornare un template CloudFormation

> ⚠️ **Attenzione**: modificare un template CFN su un'infrastruttura condivisa può causare rollback se le risorse esistono già in stato diverso. Verificare sempre le modifiche prima di deployare.

### 1. Modifica il template YAML

```
deployment/infrastructure/quota-admin-dashboard.yaml
deployment/infrastructure/user-dashboard.yaml
```

### 2. Valida il template prima di applicarlo

```bash
aws cloudformation validate-template \
  --template-body file://deployment/infrastructure/quota-admin-dashboard.yaml \
  --region eu-central-1 \
  --profile poste-claudeaws
```

### 3. Deploy dello stack

```bash
aws cloudformation deploy \
  --stack-name claude-code-quota-admin \
  --template-file deployment/infrastructure/quota-admin-dashboard.yaml \
  --capabilities CAPABILITY_IAM \
  --region eu-central-1 \
  --profile poste-claudeaws \
  --no-fail-on-empty-changeset
```

### ⚠️ Errore comune: route API Gateway già esistente (409 ConflictException)

Se il deploy CFN va in rollback con errore tipo:
```
Route with key GET /api/users already exists for this API
```

Significa che la route esiste già fuori dalla gestione dello stack. In questo caso:
- **Non ritentare il deploy CFN** — è inutile e rischioso
- Se la modifica riguarda solo Lambda o SPA, usare gli script di deploy (che saltano CFN)
- Se serve davvero modificare la route, eliminarla manualmente dalla console API Gateway prima del deploy

---

## Profilo AWS

Tutti i comandi usano il profilo `poste-claudeaws` (account `308657715154`, region `eu-central-1`).

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
