#!/usr/bin/env bash
# =============================================================================
# Déploiement automatique de la carte des arrêtés travaux (Corbas)
#
#   sudo /var/data/carteat-corbas/deploy/deploy.sh             # met à jour si main a changé
#   sudo /var/data/carteat-corbas/deploy/deploy.sh --force     # reconstruit même sans nouveauté
#   sudo /var/data/carteat-corbas/deploy/deploy.sh --reprocess # + recalcule les arrêtés importés
#   sudo /var/data/carteat-corbas/deploy/deploy.sh --check     # indique seulement s'il y a une mise à jour
#
# Étapes : récupération de main → construction de l'image → redémarrage → contrôle de santé
# → (échec : retour automatique à la version précédente) → mise à jour du snippet nginx.
# Sans nouveauté sur main, le script ne fait rien : il peut donc tourner en cron.
# Journal : /var/log/carteat-deploy.log
# =============================================================================
set -Eeuo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${BRANCH:-main}"
IMAGE="carteat-corbas"
SNIPPET_SRC="$APP_DIR/deploy/nginx-ATCorbas.conf"
SNIPPET_DST="${SNIPPET_DST:-/etc/nginx/snippets/ATCorbas.conf}"
LOG="${LOG:-/var/log/carteat-deploy.log}"
LOCK="/run/carteat-deploy.lock"
FAILED_FILE="/var/lib/carteat-deploy.failed"
HEALTH_TIMEOUT=90

FORCE=0
REPROCESS=0
CHECK_ONLY=0
NGINX=1
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --reprocess) REPROCESS=1 ;;
    --check) CHECK_ONLY=1 ;;
    --no-nginx) NGINX=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Option inconnue : $arg (voir --help)" >&2; exit 2 ;;
  esac
done

log() { printf '%s  %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }
fail() { log "ÉCHEC : $*"; exit 1; }

# ---------------------------------------------------------------- prérequis
[[ $EUID -eq 0 ]] || { echo "À lancer avec sudo." >&2; exit 1; }
command -v docker >/dev/null || fail "docker introuvable"
docker compose version >/dev/null 2>&1 || fail "docker compose (v2) introuvable"
cd "$APP_DIR"
[[ -d .git ]] || fail "$APP_DIR n'est pas un dépôt git"
[[ -f .env ]] || fail ".env absent (cp .env.example .env puis le compléter)"
grep -q '^SECRET_KEY=.' .env || fail "SECRET_KEY vide dans .env"
grep -q '^ADMIN_PASSWORD_HASH=.' .env || log "Attention : ADMIN_PASSWORD_HASH vide, l'administration sera inaccessible"

# Un seul déploiement à la fois (cron + lancement manuel).
exec 9>"$LOCK"
flock -n 9 || { echo "Un déploiement est déjà en cours." >&2; exit 0; }

HOST_PORT="$(grep -E '^HOST_PORT=' .env | tail -1 | cut -d= -f2 | tr -d '"'"'"' ' || true)"
HOST_PORT="${HOST_PORT:-8085}"
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

# ---------------------------------------------------------------- mise à jour du code
git diff --quiet && git diff --cached --quiet \
  || fail "modifications locales non committées dans $APP_DIR (git status) : déploiement annulé"
OLD="$(git rev-parse HEAD)"
git fetch --quiet origin "$BRANCH"
NEW="$(git rev-parse "origin/$BRANCH")"

if [[ "$OLD" == "$NEW" && $FORCE -eq 0 ]]; then
  if [[ $CHECK_ONLY -eq 1 ]]; then echo "À jour ($(git log -1 --format='%h %s' "$OLD"))."; fi
  exit 0
fi
if [[ $CHECK_ONLY -eq 1 ]]; then
  echo "Mise à jour disponible :"; git log --oneline "$OLD..$NEW"; exit 0
fi
# Une version déjà tombée en échec n'est pas retentée à chaque passage du cron (sauf --force).
if [[ -f "$FAILED_FILE" && "$(cat "$FAILED_FILE")" == "$NEW" && $FORCE -eq 0 ]]; then
  exit 0
fi

log "=== Déploiement $(git log -1 --format=%h "$OLD") → $(git log -1 --format=%h "$NEW")"
git log --oneline "$OLD..$NEW" | sed 's/^/    /' | tee -a "$LOG" || true
git merge --ff-only --quiet "origin/$BRANCH" || fail "historique divergent : fusion rapide impossible"

# ---------------------------------------------------------------- construction et redémarrage
# L'image en service est conservée sous l'étiquette :previous pour un éventuel retour arrière.
if docker image inspect "$IMAGE:latest" >/dev/null 2>&1; then docker tag "$IMAGE:latest" "$IMAGE:previous"; fi

rollback() {
  log "Retour à la version précédente ($(git log -1 --format=%h "$OLD"))"
  echo "$NEW" >"$FAILED_FILE"
  git reset --quiet --hard "$OLD"
  if docker image inspect "$IMAGE:previous" >/dev/null 2>&1; then
    docker tag "$IMAGE:previous" "$IMAGE:latest"
    docker compose up -d --no-build >>"$LOG" 2>&1 || true
  fi
  fail "$1"
}

log "Construction de l'image…"
docker compose build --pull >>"$LOG" 2>&1 || rollback "construction de l'image (voir $LOG)"
log "Redémarrage du conteneur…"
docker compose up -d >>"$LOG" 2>&1 || rollback "démarrage du conteneur (voir $LOG)"

log "Contrôle de santé sur 127.0.0.1:$HOST_PORT…"
ok=0
for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
  if curl -fsS -m 3 "http://127.0.0.1:$HOST_PORT/healthz" >/dev/null 2>&1 \
     && curl -fsS -m 5 "http://127.0.0.1:$HOST_PORT/api/meta" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 1
done
if [[ $ok -ne 1 ]]; then
  docker compose logs --tail=50 >>"$LOG" 2>&1 || true
  rollback "l'application ne répond pas après ${HEALTH_TIMEOUT}s (journaux du conteneur dans $LOG)"
fi
log "Application en ligne."

# ---------------------------------------------------------------- nginx
if [[ $NGINX -eq 1 && -f "$SNIPPET_SRC" && -d "$(dirname "$SNIPPET_DST")" ]]; then
  if ! cmp -s "$SNIPPET_SRC" "$SNIPPET_DST"; then
    log "Mise à jour du snippet nginx…"
    BACKUP=""
    if [[ -f "$SNIPPET_DST" ]]; then BACKUP="$(mktemp)"; cp "$SNIPPET_DST" "$BACKUP"; fi
    cp "$SNIPPET_SRC" "$SNIPPET_DST"
    if nginx -t >>"$LOG" 2>&1; then
      systemctl reload nginx && log "nginx rechargé."
    else
      if [[ -n "$BACKUP" ]]; then cp "$BACKUP" "$SNIPPET_DST"; else rm -f "$SNIPPET_DST"; fi
      log "Attention : nouvelle configuration nginx refusée (nginx -t), ancienne version conservée."
    fi
    if [[ -n "$BACKUP" ]]; then rm -f "$BACKUP"; fi
  fi
fi

# ---------------------------------------------------------------- options
if [[ $REPROCESS -eq 1 ]]; then
  log "Recalcul des arrêtés (hors corrections manuelles)…"
  if docker compose exec -T carteat python -m carteat.cli reprocess >>"$LOG" 2>&1; then
    log "Recalcul terminé."
  else
    log "Attention : le recalcul a échoué (voir $LOG)"
  fi
fi

rm -f "$FAILED_FILE"
docker image prune -f >/dev/null 2>&1 || true
log "=== Déploiement terminé : $(git log -1 --format='%h %s')"
