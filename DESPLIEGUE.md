# Despliegue 24/7 en Google Cloud

El servicio continuo (`scheduler.py`) corre en una VM de Compute Engine para no depender de que el
PC esté encendido. Esta guía rehace el despliegue desde cero. **Ningún secreto va en el repositorio**:
el `.env` y `portfolio.db` se crean o se suben a mano en la VM.

## Máquina

| | |
|---|---|
| Sistema | Debian (imagen por defecto de Compute Engine) |
| Tipo | Máquina pequeña de ~1 GB de RAM (p. ej. `e2-micro`) |
| Disco | 30 GB (el bot usa muy poco) |
| Acceso | Botón **SSH** de la consola (pasa por IAP: necesita la regla de cortafuegos `tcp:22` desde `35.235.240.0/20`) |

Con 1 GB la memoria es el cuello de botella: el bot ocupa unos 250 MB. Hacen falta los dos ajustes
del paso 2, o la VM acaba sin memoria, sin red y sin SSH.

## 1. Código, entorno y secretos

```bash
git clone https://github.com/omarquiles1/Sistema-multiagente-de-gestion-financiera.git
cd Sistema-multiagente-de-gestion-financiera
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env      # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
chmod 600 .env
```

`ant auth login` no está en la VM: hace falta una `ANTHROPIC_API_KEY` propia, creada en
console.anthropic.com.

**Cartera**: sube `portfolio.db` con **SUBIR ARCHIVO** de la terminal SSH (lo deja en `~/`) y
muévelo con `mv ~/portfolio.db .`. Sin él se empieza con la base de datos vacía. A partir de ese
momento la cartera de verdad es la de la VM: `/comprar` y `/vender` solo escriben ahí.

## 2. Memoria

```bash
# El Ops Agent de Google (métricas para Cloud Monitoring) ocupa ~265 MB; el bot no lo necesita
sudo systemctl disable --now google-cloud-ops-agent

# 2 GB de swap como colchón
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h                                # Swap: 2.0Gi
```

## 3. Servicio

`deploy/bot-finanzas.service` arranca el bot con la VM y lo relanza a los 30 s si se cae:

```bash
sed "s/USUARIO/$USER/g" deploy/bot-finanzas.service | sudo tee /etc/systemd/system/bot-finanzas.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now bot-finanzas
```

Al cabo de unos segundos, `journalctl -u bot-finanzas -n 20` debe mostrar
`Servicio iniciado. Próxima ejecución: ...` y `Listener de Telegram escuchando comandos`.
Para comprobarlo de punta a punta, manda `/cartera` al bot.

| Para… | Comando |
|---|---|
| Ver lo que hace, en directo | `journalctl -u bot-finanzas -f` |
| Ver si está encendido | `systemctl status bot-finanzas` |
| Recargar el `.env` o código nuevo (`git pull`) | `sudo systemctl restart bot-finanzas` |
| Pararlo | `sudo systemctl stop bot-finanzas` |

**Un solo bot por token.** Telegram solo entrega cada mensaje a un lector: si el bot se arranca
también en el PC, los dos se pisan y los comandos se pierden. Con el servicio en la VM, no se lanza
`scheduler.py` ni `telegram_listener.py` en local.

## Problemas conocidos

| Síntoma | Causa | Arreglo |
|---|---|---|
| Las respuestas dicen "modo matemático (IA de Claude desconectada…)" | La API de Anthropic falla: clave mal pegada, revocada o sin saldo | Revisar `ANTHROPIC_API_KEY` y el saldo; `sudo systemctl restart bot-finanzas` |
| `python notifier.py` termina al instante sin error | `notifier.py` es una librería, no un programa | El servicio es `scheduler.py` |
| El SSH se queda en "Estableciendo conexión…" o da el error 4003 de IAP | La VM se quedó sin memoria y sin red | En la consola: **DETENER** y luego **INICIAR** (no se pierde el disco); revisar el paso 2 |
| `network is unreachable` en **Registros → Puerto serie 1** | Lo mismo: la VM perdió la red | Igual que el anterior |

El bot se quedó así unos 9 días en noviembre de 2026, cuando corría en `tmux` y sin swap: la VM
se quedó sin memoria y nadie lo volvió a encender. El servicio y el paso 2 evitan que se repita.
