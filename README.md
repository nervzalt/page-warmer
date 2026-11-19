# page-warmer

Keeps a website's page cache warm. Every 5 minutes it checks a few pages; if the cache went cold, it loads every
page once as a desktop browser and once as a phone, gently (2 at a time, 1.5 s pause), and stops at once if the
site's firewall pushes back. Which site and pages come from an encrypted secret.
