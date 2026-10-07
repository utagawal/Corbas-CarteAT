# Consignes pour Claude

- **Commandes shell : toujours sur une seule ligne.** Chaque commande donnée à l'utilisateur doit pouvoir être copiée-collée telle quelle dans un terminal, en une ligne (enchaîner avec `&&` si plusieurs étapes, pas de `\` de continuation ni de bloc multi-ligne). Pour créer un fichier, préférer une copie (`cp`) depuis le dépôt plutôt qu'un contenu à coller.
- Langue de travail : français.
- Serveur de production : `maps.utagawavtt.com`, code dans `/var/data/carteat-corbas`, nginx + Cloudflare (voir `deploy/nginx-ATCorbas.conf` et le README).
