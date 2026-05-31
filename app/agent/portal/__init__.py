"""Compilazione assistita di portali web (agent_mode='portal_fill').

Riempie i campi di un form su un portale esterno a partire dai dati di una riga
di un foglio collaborativo. Riusa le primitive a basso livello dell'engine social
(humanize.human_type/human_click, session_manager per lo storage_state) ma SENZA
accoppiarsi al concetto di SocialAccount: un portale e' solo un URL + una sessione
loggata salvata su disco.
"""
