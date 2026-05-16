"""AgentScraper — package init.

Esporta `__version__` come single source of truth della versione del prodotto.
Usato da:
- `app.release_check` per confrontare con la release più recente su GitHub
- banner in `base.html` (mostra versione corrente)
- log al boot
"""
__version__ = "1.0.0"
